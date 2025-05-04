# ──────────────────────────────────────────────────────────────
#  app.py  –  Streamlit YouTube-Transcript Downloader + Chatbot
# ──────────────────────────────────────────────────────────────
import os
import streamlit as st
from datetime import date
from urllib.parse import urlparse, parse_qs
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
)
from supabase import create_client, Client, AuthApiError
import openai

# ╭─────────────────── Load secrets safely ───────────────────╮
try:  # 🖥 LOCAL - uses ignored config.py
    from config import SUPABASE_URL, SUPABASE_KEY, OPENAI_KEY
except ModuleNotFoundError:  # ☁ Cloud - uses Secrets / env-vars
    SUPABASE_URL = st.secrets.get("SUPABASE_URL", os.getenv("SUPABASE_URL"))
    SUPABASE_KEY = st.secrets.get("SUPABASE_KEY", os.getenv("SUPABASE_KEY"))
    OPENAI_KEY   = st.secrets.get("OPENAI_KEY",   os.getenv("OPENAI_KEY"))

missing = [n for n, v in {
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_KEY": SUPABASE_KEY,
    "OPENAI_KEY":   OPENAI_KEY,
}.items() if not v]
if missing:
    raise RuntimeError(f"Missing secrets: {', '.join(missing)}")
# ╰────────────────────────────────────────────────────────────╯

# ─── Initialise clients ──────────────────────────────────────
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
oa = openai.OpenAI(api_key=OPENAI_KEY)  # OpenAI SDK ≥ 1.0

# ─── Helper functions ────────────────────────────────────────
def youtube_id(url: str):
    q = urlparse(url)
    if q.hostname == "youtu.be":
        return q.path[1:]
    if q.hostname in ("www.youtube.com", "youtube.com") and q.path == "/watch":
        return parse_qs(q.query).get("v", [None])[0]
    return None

def yt_transcript(vid: str):
    try:
        return YouTubeTranscriptApi.get_transcript(vid)
    except (TranscriptsDisabled, NoTranscriptFound):
        return None

def save_transcript(vid: str, tr: list[dict]):
    supabase.table("youtube_transcripts").insert(
        dict(
            video_id=vid,
            title=f"Video {vid}",
            transcript_text="\n".join(c["text"] for c in tr),
            transcript_json=tr,
        )
    ).execute()

def profile(uid: str):
    return (supabase.table("user_profile")
            .select("*").eq("id", uid).single().execute()).data

def bump_counter(uid: str):
    today = str(date.today())
    supabase.table("user_profile").update(
        dict(daily_chat_count=1, last_chat_date=today)
    ).eq("id", uid).execute()

# ─── Login / Sign-up UI ──────────────────────────────────────
if "user" not in st.session_state:
    tab_login, tab_signup = st.tabs(["Login", "Sign-up"])

    with tab_login:
        email = st.text_input("Email")
        pw    = st.text_input("Password", type="password")
        if st.button("Login"):
            try:
                st.session_state.user = supabase.auth.sign_in_with_password(
                    dict(email=email, password=pw)
                ).user
                st.rerun()
            except AuthApiError as err:
                st.error(err.message)

    with tab_signup:
        fullname = st.text_input("Full name")
        email_s  = st.text_input("Email (sign-up)")
        pw_s     = st.text_input("Password", type="password")
        if st.button("Create account"):
            supabase.auth.sign_up(
                dict(email=email_s, password=pw_s,
                     options={"data": {"full_name": fullname}})
            )
            st.success("Check your inbox to confirm e-mail.\nWait for admin approval.")
    st.stop()

user = st.session_state.user
prof = profile(user.id)
if not prof or not prof["approved"]:
    st.warning("⏳  Awaiting admin approval…")
    st.stop()

# ─── Main app ────────────────────────────────────────────────
st.title("📺  YouTube Transcript App")
mode = st.sidebar.radio("Mode", ("Downloader", "Chatbot"))

# ▼ Downloader ────────────────────────────────────────────────
if mode == "Downloader":
    st.header("📥  Download transcripts")
    links_text = st.text_area("YouTube links (one per line)")
    if st.button("Fetch"):
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

# ▼ Chatbot ───────────────────────────────────────────────────
else:
    if not prof["can_chat"]:
        st.info("🚫  Chatbot not enabled for your account.")
        st.stop()

    # daily quota ≤ 2
    if prof["last_chat_date"] == str(date.today()) and prof["daily_chat_count"] >= 2:
        st.warning("Daily quota (2 questions) reached. Come back tomorrow.")
        st.stop()

    rows = supabase.table("youtube_transcripts").select("video_id", "title").execute().data
    if not rows:
        st.info("No transcripts stored yet."); st.stop()

    label = st.selectbox(
        "Choose a video",
        [f"{r['title']} ({r['video_id']})" for r in rows]
    )
    vid = label.split("(")[-1][:-1]  # extract id

    question = st.text_input("Ask your question")
    if question:
        tx = (supabase.table("youtube_transcripts")
              .select("transcript_text")
              .eq("video_id", vid).single().execute()).data["transcript_text"]

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

