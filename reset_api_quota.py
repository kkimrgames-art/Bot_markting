import asyncio
from dotenv import load_dotenv

load_dotenv()

from src.agent.youtube_api_keys import get_key_manager, _lock
from src.agent.supabase_client import supabase_upsert

def reset_quota():
    print("🚀 بدء فحص وإعادة تعيين حصص API...")
    
    mgr = get_key_manager()
    mgr._load_keys()
    
    keys = mgr.list_keys()
    if not keys:
        print("⚠️ لا يوجد أي مفتاح API مسجل في قاعدة البيانات.")
        return
        
    print(f"📊 تم العثور على {len(keys)} مفتاح. جاري تصفير الحصص المستهلكة وتفعيلها...")
    
    count = 0
    for k in keys:
        try:
            key_id = k['key_id']
            print(f"- تصفير المفتاح: {k['label']} ({key_id})")
            
            # Update the local manager data structure
            with _lock:
                target_key = None
                for db_k in mgr._keys:
                    if db_k["key_id"] == key_id:
                        db_k["quota_used"] = 0
                        db_k["is_active"] = True
                        target_key = dict(db_k) # Copy it for pushing to DB
                        
            if target_key:
                # Upsert to DB with full data to prevent null constraints
                supabase_upsert("youtube_api_keys", target_key, key_field="key_id")
                count += 1
                
        except Exception as e:
            print(f"❌ Error updating key {key_id}: {e}")
            
    print(f"✅ تم إعادة تعيين {count} مفاتيح بنجاح.")
    
if __name__ == "__main__":
    reset_quota()
