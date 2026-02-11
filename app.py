import json
import requests
import os
import wave
import base64
from requests.auth import HTTPBasicAuth
import subprocess

from agentapps import Agent, Tool
from agentapps.model import GrokChat

from vosk import Model, KaldiRecognizer
from pydub import AudioSegment


# =====================================================
# 🔧 FFMPEG CONFIG (MANDATORY ON WINDOWS)
# =====================================================

FFMPEG_PATH = r"C:\ffmpeg\bin\ffmpeg.exe"
FFPROBE_PATH = r"C:\ffmpeg\bin\ffprobe.exe"

os.environ["PATH"] += os.pathsep + r"C:\ffmpeg\bin"


AudioSegment.converter = FFMPEG_PATH
AudioSegment.ffprobe = FFPROBE_PATH

# Validate ffmpeg + ffprobe
subprocess.run([FFMPEG_PATH, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
subprocess.run([FFPROBE_PATH, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

print("✅ FFmpeg & FFprobe validated")

# =====================================================
# 🔐 JIRA CONFIG
# =====================================================
JIRA_EMAIL = "mail id associated with jira account"
JIRA_API_TOKEN = "your_jira_api_token_here"
JIRA_BASE_URL = "your id .atlassian.net"

AUTH = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json"
}

def jira_auth_header():
    token = f"{JIRA_EMAIL}:{JIRA_API_TOKEN}"
    encoded = base64.b64encode(token.encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "X-Atlassian-Token": "no-check"
    }

# =====================================================
# 1️⃣ FETCH MP3 ATTACHMENTS
# =====================================================
class FetchJiraMp3Tool(Tool):
    def __init__(self):
        super().__init__(
            name="fetch_jira_mp3",
            description="Fetch MP3 attachments from Jira issue"
        )

    def execute(self, issue_key: str):
        url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}"
        r = requests.get(url, headers=HEADERS, auth=AUTH)

        if r.status_code != 200:
            return "❌ Failed to fetch Jira issue"

        attachments = r.json()["fields"]["attachment"]
        mp3s = [
            {"filename": a["filename"], "url": a["content"]}
            for a in attachments
            if a["filename"].lower().endswith(".mp3")
        ]

        if not mp3s:
            return "❌ No MP3 attachments found"

        return json.dumps(mp3s, indent=2)

    def get_parameters(self):
        return {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"}
            },
            "required": ["issue_key"]
        }

# =====================================================
# 2️⃣ DOWNLOAD MP3
# =====================================================
class DownloadMp3Tool(Tool):
    def __init__(self):
        super().__init__(
            name="download_mp3",
            description="Download MP3 attachment from Jira"
        )

    def execute(self, url: str, filename: str):
        url = url.replace("/rest/api/3/", "/rest/api/2/")
        headers = jira_auth_header()

        r = requests.get(url, headers=headers, stream=True)
        if r.status_code != 200:
            return f"❌ MP3 download failed ({r.status_code})"

        file_path = os.path.abspath(filename)

        with open(file_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        return file_path

    def get_parameters(self):
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "filename": {"type": "string"}
            },
            "required": ["url", "filename"]
        }

# =====================================================
# 3️⃣ OFFLINE TRANSCRIPTION (VOSK)
# =====================================================

class FreeTranscriptionTool(Tool):
    def __init__(self):
        super().__init__(
            name="transcribe_audio",
            description="Offline MP3 to text using VOSK"
        )
        self.model = Model("vosk-model-small-en-us-0.15")

    def execute(self, filename: str):
        filename = os.path.abspath(filename)

        print("CWD:", os.getcwd())
        print("Transcribing:", filename)
        print("Exists:", os.path.exists(filename))

        if not os.path.exists(filename):
            return "❌ Audio file not found"

        # -----------------------------
        # Convert MP3 → WAV (PCM 16-bit)
        # -----------------------------
        wav_file = filename.replace(".mp3", ".wav")

        audio = AudioSegment.from_file(filename, format="mp3")
        audio = audio.set_channels(1)
        audio = audio.set_frame_rate(16000)
        audio = audio.set_sample_width(2)

        audio.export(wav_file, format="wav")

        # -----------------------------
        # VOSK transcription
        # -----------------------------
        wf = wave.open(wav_file, "rb")
        rec = KaldiRecognizer(self.model, wf.getframerate())
        rec.SetWords(True)

        transcript_parts = []

        while True:
            data = wf.readframes(4000)
            if len(data) == 0:
                break
            if rec.AcceptWaveform(data):
                result = json.loads(rec.Result())
                transcript_parts.append(result.get("text", ""))

        final_result = json.loads(rec.FinalResult())
        transcript_parts.append(final_result.get("text", ""))

        wf.close()

        transcript = " ".join(transcript_parts).strip()

        if not transcript:
            return "⚠️ No speech detected in audio"

        return transcript

    def get_parameters(self):
        return {
            "type": "object",
            "properties": {
                "filename": {"type": "string"}
            },
            "required": ["filename"]
        }



# =====================================================
# 4️⃣ UPDATE JIRA WORK NOTES
# =====================================================
class UpdateJiraWorkNotesTool(Tool):
    def __init__(self):
        super().__init__(
            name="update_jira_worknotes",
            description="Update Jira issue with voice analysis"
        )

    def execute(self, issue_key: str, notes: str):
        url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/comment"

        payload = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [{
                    "type": "paragraph",
                    "content": [{"type": "text", "text": notes}]
                }]
            }
        }

        r = requests.post(url, headers=HEADERS, json=payload, auth=AUTH)
        if r.status_code not in (200, 201):
            return "❌ Failed to update Jira"

        return "✅ Jira work notes updated"

    def get_parameters(self):
        return {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "notes": {"type": "string"}
            },
            "required": ["issue_key", "notes"]
        }

# =====================================================
# 🤖 MODEL
# =====================================================
grok_model = GrokChat(
    id="grok-3-mini",
    api_key="your_grok_api_key_here"
)

# =====================================================
# 🤝 AGENTS
# =====================================================
fetch_agent = Agent(
    name="Fetch Agent",
    role="Fetch MP3 attachments",
    model=grok_model,
    tools=[FetchJiraMp3Tool()]
)

download_agent = Agent(
    name="Download Agent",
    role="Download audio file",
    model=grok_model,
    tools=[DownloadMp3Tool()]
)

transcript_agent = Agent(
    name="Transcription Agent",
    role="Convert voice to text",
    model=grok_model,
    tools=[FreeTranscriptionTool()]
)

update_agent = Agent(
    name="Analysis & Update Agent",
    role="Analyze transcript and update Jira work notes",
    model=grok_model,
    tools=[UpdateJiraWorkNotesTool()]
)

# =====================================================
# 🧩 JIRA VOICE AGENT TEAM
# =====================================================
jira_voice_team = Agent(
    team=[
        fetch_agent,
        download_agent,
        transcript_agent,
        update_agent
    ],
    instructions=[
        "Fetch MP3 attachment from Jira issue",
        "Download the audio file",
        "Transcribe the discussion",
        "Analyze the transcript and extract summary, issues, decisions, and action items",
        "Update Jira work notes with structured analysis"
    ],
    show_tool_calls=True
)

# =====================================================
# ▶️ RUN
# =====================================================
if __name__ == "__main__":
    while True:
        issue_key = input("\nEnter Jira Issue Key (or exit): ")
        if issue_key.lower() in ("exit", "quit"):
            break

        jira_voice_team.print_response(
            f"""
            Process the voice attachment for Jira issue {issue_key}.
            Analyze the discussion and update Jira work notes with:
            - Summary
            - Key issues
            - Decisions
            - Action items
            """
        )

