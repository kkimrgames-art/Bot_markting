import os
import random
import hashlib
from typing import List, Optional, Dict
from dataclasses import dataclass

@dataclass
class StyleProfile:
    name: str
    tone: str
    emoji_style: str
    structure_pref: str  # e.g., "standard", "story", "minimal"

@dataclass
class PromptSection:
    name: str
    required: bool
    position_weight: int  # invalid for shuffled logic, but useful for 'standard' sorting

class PromptTemplates:
    STYLES = {
        "professional": StyleProfile(
            "Professional",
            "Informative, clear, professional, authoritative, educational",
            "Minimal, relevant usage (1-3 emojis)",
            "standard"
        ),
        "gamer": StyleProfile(
            "Gamer",
            "Enthusiastic, hype, uses gaming slang, community-focused, energetic",
            "High usage, gaming-themed (🎮, 🔥, 🚀)",
            "hook_first"
        ),
        "storyteller": StyleProfile(
            "Storyteller",
            "Narrative, engaging, builds suspense, personal connection",
            "Emotional key points",
            "story"
        ),
        "friend": StyleProfile(
            "Friend",
            "Casual, friendly, chatty, direct address ('you guys')",
            "Friendly faces, hearts, glitter",
            "casual"
        ),
        "clicky": StyleProfile(
            "Hype/Viral",
            "Urgent, questioning, viral-focused, curiosity-inducing",
            "Alerts, arrows, fire (⚠️, ⬇️, 😱)",
            "hook_first"
        )
    }

    SECTIONS = [
        "HOOK_INTRO",    # Catchy opening sentence
        "TITLE_SECTION", # Reference to the video title
        "MAIN_CONTENT",  # The meat of the description
        "CTA_APP",       # Call to action for the app
        "CTA_SUB",       # Call to action for subscribing/liking
        "HASHTAGS",      # Hashtag block
        "KEYWORDS",      # SEO Keywords block
        "CREDITS",       # Credits if applicable
        "FUN_FACT",      # A random related fun fact (new idea for uniqueness)
    ]

    @staticmethod
    def _get_random_style(seed: str) -> str:
        keys = list(PromptTemplates.STYLES.keys())
        # Use simple hash for deterministic "randomness" based on seed
        h = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16)
        return keys[h % len(keys)]

    @staticmethod
    def _get_structure_instructions(style_key: str, seed: str) -> str:
        """
        Returns instructions on how to structure the description based on style and randomization.
        """
        style = PromptTemplates.STYLES.get(style_key, PromptTemplates.STYLES["professional"])
        
        # Base components
        components = [
            "1. **Opening Hook/Intro**: 2-3 sentences.",
            "2. **Video Title Reference**: Mention the title clearly.",
            "3. **Main Content/Features**: Bullet points or paragraph.",
            "4. **App Download CTA**: Must include the provided app link.",
            "5. **Engagement CTA**: Ask to like/subscribe.",
            "6. **Hashtags**: 10-15 tags.",
            "7. **Keywords**: Comma-separated list."
        ]
        
        # Shuffle logic for variety, but keep App CTA and Hashtags/Keywords generally towards the end
        # We can define a few "Presets"
        presets = [
            # Preset A: Standard
            [0, 1, 2, 4, 3, 5, 6],
            # Preset B: App First (Aggressive)
            [0, 3, 1, 2, 4, 5, 6],
            # Preset C: Content First
            [2, 1, 0, 4, 3, 5, 6],
            # Preset D: Engagement First
            [4, 0, 1, 2, 3, 5, 6],
        ]
        
        h = int(hashlib.sha256((seed + "_struct").encode("utf-8")).hexdigest(), 16)
        chosen_preset_idx = h % len(presets)
        chosen_order = presets[chosen_preset_idx]
        
        ordered_instructions = []
        for i, idx in enumerate(chosen_order):
            ordered_instructions.append(components[idx])
            
        return "\n".join(ordered_instructions)

    @staticmethod
    def get_dynamic_prompt_instruction(
        topic: str,
        title: str,
        app_link: str,
        lang: str,
        keywords: Optional[List[str]] = None,
        style_seed: Optional[str] = None
    ) -> str:
        if not style_seed:
            style_seed = f"{topic}|{title}|{lang}"
            
        style_key = PromptTemplates._get_random_style(style_seed)
        style = PromptTemplates.STYLES[style_key]
        structure_instr = PromptTemplates._get_structure_instructions(style_key, style_seed)
        
        keywords_str = ", ".join(keywords) if keywords else ""
        
        prompt = (
            f"Task: Write a unique, effective YouTube description in {lang}.\n"
            f"Topic: {topic}\n"
            f"Video Title: {title}\n"
            f"Details/Keywordss: {keywords_str}\n"
            f"App Link: {app_link}\n\n"
            f"**Persona/Style**: {style.name}\n"
            f"Tone: {style.tone}\n"
            f"Emoji Usage: {style.emoji_style}\n\n"
            "**Structure Constraints** (Follow this order):\n"
            f"{structure_instr}\n\n"
            "**General Rules**:\n"
            "- VARY the phrasing. Do not use the same opening every time.\n"
            "- For the App CTA, vary the call to action (e.g., 'Get the mod here', 'Download now', 'Try it yourself').\n"
            "- Ensure the text is naturally readable and engaging.\n"
            "- Return ONLY the description text."
        )
        return prompt

    @staticmethod
    def get_shorts_prompt_instruction(
        topic: str,
        title: str,
        lang: str,
        style_seed: Optional[str] = None
    ) -> str:
        if not style_seed:
            style_seed = f"{topic}|{title}|{lang}"
            
        style_key = PromptTemplates._get_random_style(style_seed)
        style = PromptTemplates.STYLES[style_key]
        required_tag = (os.getenv("SHORTS_REQUIRED_TAG") or "").strip()
        if required_tag and not required_tag.startswith("#"):
            required_tag = "#" + required_tag
        rules = []
        if required_tag:
            rules.append(
                f"1. **OPTIONAL BRAND TAG**: Include the tag `{required_tag}` in BOTH the Title and Description. Keep it EXACTLY as provided."
            )
            rules.append(
                f"2. **LANGUAGE**: Use ONLY {lang} for ALL other hashtags and keywords. The ONLY allowed exception is `{required_tag}`."
            )
            title_example = f"#Minecraft #Mods {required_tag}"
        else:
            rules.append(f"1. **LANGUAGE**: Use ONLY {lang} for ALL hashtags and keywords. Do NOT use any other language.")
            title_example = "#Minecraft #Mods #Shorts"
        
        prompt = (
            f"Target Language: {lang}\n"
            f"Task: Generate a VIRAL YouTube Shorts metadata set optimized for SEARCH (SEO).\n"
            f"Topic: {topic}\n"
            f"Base Title: {title}\n\n"
            "**CRITICAL RULES**:\n"
            + "\n".join(rules)
            + "\n\n"

            "**Structure Requirements**:\n"
            f"1. **Title**: MUST be composed of 2-6 POWERFUL hashtags. NO plain text sentences. Example: `{title_example}`.\n"
            "2. **Description**: Hashtags + SEO keywords ONLY (no sentences). Focus on searchable terms supported by the source title/topic.\n"
            "3. **Content Relevance**: Use ONLY hashtags clearly justified by the provided topic/title. Do NOT add generic defaults like #gaming, #video, #mods, or #shorts unless the source really supports them. Prefer specific game/event/object/action tags over broad categories.\n"
            "4. **Hashtag Block**: Additional 8-14 topic-specific tags, not a repeated generic bundle.\n\n"

            "Return exactly in this format:\n"
            "Title: <hashtags only>\n"
            "Description: <seo block>\n"
            "Hashtags: <...>"
        )
        return prompt

    @staticmethod
    def get_mod_data_prompt_instruction(
        raw_data: str,
        lang: str,
        title: str,
        keywords: Optional[List[str]] = None,
        style_seed: Optional[str] = None,
        app_link: Optional[str] = None
    ) -> str:
        if not style_seed:
            style_seed = f"{lang}|{len(raw_data)}|{title}"
            
        style_key = PromptTemplates._get_random_style(style_seed)
        style = PromptTemplates.STYLES[style_key]
        structure_instr = PromptTemplates._get_structure_instructions(style_key, style_seed)
        
        keywords_str = ", ".join(keywords) if keywords else ""
        
        prompt = (
            f"Task: Write a COMPREHENSIVE and UNIQUE YouTube video description in {lang} based on the provided Mod Data.\n"
            f"Video Title: {title}\n"
            f"Keywords: {keywords_str}\n"
            f"App Link: {app_link or 'Link in description'}\n\n"
            f"**Persona/Style**: {style.name}\n"
            f"Tone: {style.tone}\n\n"
            "**Structure Constraints** (Follow this order):\n"
            f"{structure_instr}\n\n"
            "**Data Usage Rules**:\n"
            "- Extract key features, installation steps, and compatibility from the Raw Mod Data below.\n"
            "- Do NOT copy-paste. Rewrite in the target language and requested style.\n"
            "- Ignore irrelevant data (logs, raw code, etc).\n\n"
            "**Raw Mod Data**:\n"
            f"{raw_data[:3000]}\n" # Limit raw data to avoid token overflow
        )
        return prompt
