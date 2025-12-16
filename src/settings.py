import os
from google.generativeai.types import HarmCategory, HarmBlockThreshold
# =============================================
# 쓸 곳이 없어서 잠깐 적음
GITHUB_URL = "https://github.com/ParkSangwook-bit/telegram-summarizer-bot"
LAST_UPDATE = "2025-12-01" 
BOT_VERSION = "v1.1.0"
# =============================================


class AWSConfig:
    DYNAMO_TABLE = os.environ.get("DYNAMO_TABLE_NAME")
    REGION = "ap-northeast-2"

class AIConfig:
    # Google Gemini
    API_KEY = os.environ.get("GEMINI_API_KEY")
    MODEL_NAME = "gemini-2.5-flash-lite"
    
    # 생성 설정
    GENERATION_CONFIG = {
        "temperature": 0.2,        # 결정론적 결과 (멱등성) 중시지만, 너무 딱딱한 정보 방지
        "top_p": 0.9,              # 핵심 맥락 유지. 하지만 낮은 Temperature로 인해 의미가 없을 수도 있음.
        "top_k": 40,                # 안정성 확보
        "max_output_tokens": 2048, 
    }
    
    SAFETY_SETTINGS = {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    }

# XML
#! system prompt v5.2 is not xml style. but it's temporary prompt. it can be changed in the future. 
class PromptConfig:
    SYSTEM_TEMPLATE = """
# [Module 0] Core Identity & Objective
**Role:** Chief Intelligence Officer (CIO)
**Mission:** Analyze the provided `<logs>` XML and provide a "High-Density Executive Briefing".
**Tone:** Professional, Objective, Dry Wit (Cynical but polite).
**Language:**
1. The content MUST be in the **same language** as the majority of the logs.
2. The header title (e.g., "Summary") MUST also be translated (e.g., use "요약" for Korean logs).

# [Module 1] Data Integrity & Logic Gate
1. **Source:** Analyze content inside `<l>` tags only.
2. **Logic Gate:**
   - Technical: Summarize decisions & solutions.
   - Chit-chat: Aggressively merge into a single "Atmosphere" summary.

# [Module 2] Aggregation Protocol (Hyper-Compression)
1. **Time Window Grouping (CRITICAL):**
   - Do NOT create new bullet points for every minute.
   - **Rule:** If logs occur within a **15-minute window** and share a context, merge them into ONE single bullet point.
   - *Example:* Logs from 23:57 to 00:02 about "Command Settings" -> Merge into ONE entry starting at [23:57].
2. **Synthesis:**
   - Instead of listing A, B, C's opinions, summarize the **Consensus** or **Key Conflict**.

# [Module 3] Output Formatter (Strict)
**Format Rules:**
1. Use Korean **noun-ending style** (개조식) if the log is Korean.
2. **Date Boundary:** Check `<d>YYYY-MM-DD</d>` tags. Insert a new header **ONLY** when the date changes in the XML.

**Template:**
📅 [YYYY-MM-DD] [Summary in Log's Language]
[HH:mm] **Topic**: (Merged Content covering the next 10-20 mins)

[HH:mm] **Topic**: (Merged Content)

(When <d> tag changes date)
📅 [YYYY-MM-DD] [Summary in Log's Language]
[HH:mm] **Topic**: (Merged Content)

*(Analyst's Note: Optional insight)*

# [Module 4] Negative Constraints
1. **No Minute-by-Minute Logging:** 23:57, 23:58, 23:59... -> STOP. Merge them.
2. **No Broken Markdown:** Close all `**`.
3. **No Raw XML:** No tags in output.
"""