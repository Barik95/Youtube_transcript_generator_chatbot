import os, streamlit as st
from datetime import date
from urllib.parse import urlparse, parse_qs
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from supabase import create_client, Client, AuthApiError
import openai, httpx

# ╭────────── Load secrets ───────────╮
try:
    from config import SUPABASE_URL, SUPABASE_KEY, OPENAI_KEY
except ModuleNotFoundError:
    SUPABASE_URL = st.secrets.get("SUPABASE_URL", os.getenv("SUPABASE_URL"))
    SUPABASE_KEY = st.secrets.get("SUPABASE_KEY", os.getenv("SUPABASE_KEY"))
    OPENAI_KEY   = st.secrets.get("OPENAI_KEY", os.getenv("OPENAI_KEY"))

missing = [n for n, v in {
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_KEY": SUPABASE_KEY,
    "OPENAI_KEY"  : OPENAI_KEY,
}.items() if not v]
if missing:
    raise RuntimeError(f"Missing secrets: {', '.join(missing)}")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
oa = openai.OpenAI(api_key=OPENAI_KEY)

# ── Helper fns ──────────────────────
def youtube_id(url: str) -> str | None:
    q = urlparse(url)
    if q.hostname == "youtu.be": return q.path[1:]
    if q.hostname in ("www.youtube.com", "youtube.com") and q.path == "/watch":
        return parse_qs(q.query).get("v", [None])[0]
    return None

def yt_transcript(vid: str):
    try: return YouTubeTranscriptApi.get_transcript(vid)
    except (TranscriptsDisabled, NoTranscriptFound): return None

def save_transcript(vid: str, tr: list[dict]):
    supabase_postgrest.table("youtube_transcripts").insert(
        dict(
            video_id=vid,
            title=f"Video {vid}",
            transcript_text="\n".join(c["text"] for c in tr),
            transcript_json=tr,
        )
    ).execute()

def profile(uid: str):
    return supabase_postgrest.table("user_profile").select("*").eq("id", uid).single().execute().data

def bump_counter(uid: str):
    supabase_postgrest.table("user_profile").update(
        dict(daily_chat_count=1, last_chat_date=str(date.today()))
    ).eq("id", uid).execute()

# ── Auth UI ─────────────────────────
if "user" not in st.session_state:
    tab_login, tab_signup = st.tabs(["Login", "Sign-up"])

    with tab_login:
        email = st.text_input("Email", key="login_email")
        pw = st.text_input("Password", type="password", key="login_pw")
        if st.button("Login", key="login_btn"):
            if not email or not pw:
                st.error("Email & password required")
            else:
                try:
                    result = supabase.auth.sign_in_with_password(
                        {"email": email, "password": pw}
                    )
                    st.session_state.user = result.user
                    st.session_state.session = result.session
                    st.success("✅ Login successful! Reloading app...")
                    st.rerun()

                except AuthApiError as err:
                    raw = getattr(err.__cause__, "response", None)
                    st.error(f"Auth error: {raw.text if raw else err.message}")

                except Exception as e:
                    import traceback
                    st.error("Unexpected error during login.")
                    st.text(traceback.format_exc())

    with tab_signup:
        fullname = st.text_input("Full name", key="signup_name")
        email_s = st.text_input("Email (sign-up)", key="signup_email")
        pw_s = st.text_input("Password", type="password", key="signup_pw")
        if st.button("Create account", key="signup_btn"):
            if not email_s or not pw_s:
                st.error("Email & password required")
            else:
                try:
                    result = supabase.auth.sign_up(
                        {"email": email_s, "password": pw_s, "options": {"data": {"full_name": fullname}}}
                    )
                    user_id = result.user.id
                    supabase.table("user_profile").insert({
                        "id": user_id,
                        "full_name": fullname,
                        "approved": False,
                        "can_chat": False,
                        "daily_chat_count": 0,
                        "last_chat_date": None,
                    }).execute()

                    st.success("Account created – wait for admin approval.")
                except AuthApiError as err:
                    raw = getattr(err.__cause__, "response", None)
                    st.error(raw.text if raw else err.message)
    st.stop()

# ── User token for postgrest ─────────
session = supabase.auth.get_session()
if session and session.access_token:
    supabase_postgrest = supabase.with_auth(session.access_token)
else:
    supabase_postgrest = supabase

# ── Approval gate ───────────────────
user = st.session_state.user
prof = profile(user.id)

if not prof or not prof["approved"]:
    st.warning("⏳ Awaiting admin approval…")
    st.stop()

# ── Main App ────────────────────────
st.title("📺  YouTube Transcript App")
mode = st.sidebar.radio("Mode", ("Downloader", "Chatbot"))

if mode == "Downloader":
    st.header("📥 Download transcripts")
    links_text = st.text_area("YouTube links (one per line)", key="link_input")
    if st.button("Fetch", key="fetch_btn"):
        for link in [l.strip() for l in links_text.splitlines() if l.strip()]:
            vid = youtube_id(link)
            if not vid:
                st.error(f"{link} → invalid"); continue
            tr = yt_transcript(vid)
            if tr:
                save_transcript(vid, tr)
                st.success(f"{link} → saved")
            else:
                st.warning(f"{link} → no transcript")

else:
    if not prof["can_chat"]:
        st.info("🚫 Chatbot not enabled for your account."); st.stop()

    if prof["last_chat_date"] == str(date.today()) and prof["daily_chat_count"] >= 2:
        st.warning("Daily quota (2 questions) reached."); st.stop()

    rows = supabase_postgrest.table("youtube_transcripts").select("video_id", "title").execute().data
    if not rows:
        st.info("No transcripts stored yet."); st.stop()

    label = st.selectbox(
        "Choose a video",
        [f"{r['title']} ({r['video_id']})" for r in rows],
        key="video_select",
    )
    vid = label.split("(")[-1][:-1]

    question = st.text_input("Ask your question", key="question_input")
    if question:
        tx = supabase_postgrest.table("youtube_transcripts").select("transcript_text").eq("video_id", vid).single().execute().data["transcript_text"]
        prompt = f"Answer only from this transcript:\n{tx}\n\nQ: {question}\nA:"
        try:
            res = oa.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "Use only the transcript."},
                    {"role": "user", "content": prompt},
                ],
            )
            ans = res.choices[0].message.content
            st.chat_message("user").write(question)
            st.chat_message("assistant").write(ans)
            bump_counter(user.id)
        except Exception as e:
            st.error(e)
