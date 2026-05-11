import asyncio
import logging
import time
import sys
import os
import multiprocessing
import signal
from typing import Optional
import json

from src.agent.job_queue import JobQueue
from src.agent.auto_mod_fetcher import AutoModFetcher, get_instance_id
from src.agent.alert_system import get_alert_system
from src.agent.disk_guard import cleanup_old_files
from src.agent.memory_guard import periodic_maintenance as mem_maintenance

logger = logging.getLogger("JobWorker")

def run_worker_process():
    """Entry point for the worker process."""
    # Ensure worker process doesn't duplicate background sync with Supabase
    os.environ["SUPABASE_DISABLE_BG_SYNC"] = "1"
    
    # Set up logging for the new process
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO,
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    
    # Set up signal handling for graceful shutdown
    stop_event = asyncio.Event()
    
    def signal_handler(signum, frame):
        logger.info(f"🛑 Worker process received signal {signum}")
        # We can't set asyncio event from signal handler directly easily in all loops,
        # but we can set a flag or raise SystemExit
        raise SystemExit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    worker = JobWorker()
    try:
        asyncio.run(worker.start())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as e:
        logger.critical(f"💥 Worker process crashed: {e}", exc_info=True)
    finally:
        logger.info("👋 Worker process exiting.")

class JobWorker:
    def __init__(self):
        self.queue = JobQueue()
        self.instance_id = get_instance_id()
        self.fetcher = None  # Lazy init
        self.running = False
        self.notifications_enabled = str(os.getenv("AUTO_MOD_WORKER_NOTIFICATIONS", "true")).strip().lower() in {"1", "true", "yes", "on"}
        self._last_notify_probe_log = 0.0

    async def _notify(self, message: str):
        """إرسال إشعارات تقدم المعالجة من العامل (Worker) إلى المسؤول."""
        if not self.notifications_enabled:
            return
        try:
            alert_system = get_alert_system()
            admin_chat_id = alert_system.get_admin_chat_id()
            if not admin_chat_id:
                now = time.time()
                if now - self._last_notify_probe_log >= 300:
                    logger.warning("⚠️ Worker notifications are disabled: admin chat is not configured yet.")
                    self._last_notify_probe_log = now
                return

            text = str(message or "").strip()
            bot_app = alert_system.get_bot_app()
            if bot_app is not None:
                await bot_app.bot.send_message(chat_id=admin_chat_id, text=text, parse_mode="Markdown")
                return

            token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
            if not token:
                return

            import aiohttp
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": admin_chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json; charset=utf-8"}
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=data, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("Worker notification failed (%s): %s", resp.status, body[:200])
        except Exception as notify_exc:
            logger.warning("⚠️ Failed to send worker notification: %s", notify_exc)

    async def start(self):
        logger.info("👷 JobWorker started. Waiting for jobs...")
        self.running = True
        
        # Initialize fetcher here (in the worker process)
        self.fetcher = AutoModFetcher(self.instance_id)

        # Reset stuck jobs on startup (jobs that were processing when previous worker died)
        # Timeout: 30 minutes (1800s).
        self.queue.reset_stuck_jobs(timeout_seconds=1800)

        while self.running:
            try:
                # 1. Maintenance
                self.queue.cleanup_completed_jobs(max_age_seconds=86400) # Keep 24h history

                # 2. Get next job
                job = self.queue.get_next_job()
                if not job:
                    await asyncio.sleep(5)  # Sleep if no jobs
                    continue

                logger.info(f"👷 Processing Job #{job['id']} (Agent: {job['agent_id']}, Type: {job['task_type']})")

                # 3. Execute Job
                start_time = time.time()
                try:
                    await self._execute_job(job)
                    self.queue.complete_job(job['id'])
                    duration = time.time() - start_time
                    logger.info(f"✅ Job #{job['id']} completed in {duration:.1f}s")
                except Exception as e:
                    logger.error(f"❌ Job #{job['id']} failed: {e}", exc_info=True)
                    self.queue.fail_job(job['id'], str(e))
                    
                    # Notify admin
                    try:
                        await get_alert_system().alert(
                            "warning",
                            f"فشل المهمة #{job['id']}",
                            f"الوكيل: `{job['agent_id']}`\nالخطأ: `{str(e)[:200]}`"
                        )
                    except Exception:
                        pass

                # 4. Cleanup after job (Resource Optimization)
                # This is crucial for Render Free Tier to release memory/disk
                cleanup_old_files()
                mem_maintenance()
                
                # Force GC? Python does it automatically, but we can be explicit if needed.
                import gc
                gc.collect()

                # 5. Wait a bit between jobs to let system cool down (Render Free Tier)
                await asyncio.sleep(5)

            except asyncio.CancelledError:
                logger.info("🛑 Worker task cancelled.")
                break
            except Exception as e:
                logger.error(f"💥 Critical Worker Error: {e}", exc_info=True)
                await asyncio.sleep(10)

    async def _execute_job(self, job):
        task_type = job['task_type']
        payload = job['payload']

        if task_type == 'process_schedule':
            # Run the cycle for this specific agent, forcing execution since it was already scheduled
            # We use force=True because the scheduler (parent process) already checked the time/limits.
            await self.fetcher.run_cycle(
                target_channel_id=payload.get('channel_id'),
                target_content_type=payload.get('content_type'),
                notify_func=self._notify,
                force=True
            )
        elif task_type == 'keep_alive':
             # Just a dummy task to verify worker is alive
             pass
        else:
            raise ValueError(f"Unknown task type: {task_type}")

    async def stop(self):
        self.running = False
        logger.info("👷 JobWorker stopping...")
