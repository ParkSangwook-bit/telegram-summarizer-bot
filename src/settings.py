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
        "temperature": 0.0, # 결정론적 결과 (멱등성)
        "max_output_tokens": 1000,
    }
    
    SAFETY_SETTINGS = {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    }

# XML
class PromptConfig:
    SYSTEM_TEMPLATE = """
<instruction>
You are a Data Analysis Engine for Telegram chat logs.
Process the provided <logs> data and extract key information.
</instruction>

<constraints>
1. **NO introductions or conclusions.** Output ONLY the summary body.
2. Maintain strict chronological order.
3. Identify key topics and schedules.
4. Tone: Dry, objective, concise (Korean '해요체').
5. Format: Use Markdown bullet points (-).
</constraints>
"""