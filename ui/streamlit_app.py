"""Streamlit chat UI. It only talks to the API (no business logic here).

HOW: shopper picker (stands in for login) -> chat box or microphone -> POST /chat -> render answer,
product cards, comparison table and the trace id. Voice: st.audio_input -> POST /transcribe -> same /chat.
WHY a thin UI: the API is the contract judges test; the UI is just one client of it.
"""
import os  # read API_URL / PUBLIC_API_URL
import uuid  # random session IDs

import requests  # HTTP calls to our FastAPI backend
import streamlit as st  # the UI framework (re-runs this whole script on every click/message)

API = os.getenv("API_URL", "http://localhost:8000")             # where the UI calls the API
LINK_API = os.getenv("PUBLIC_API_URL", API)                     # what the browser can open (differs in Docker)
TYPE_LABEL = {"recommendation": "🛍️ Recommendation", "comparison": "⚖️ Comparison", "clarification": "❓ Question",  # friendly label per answer_type
              "product_fact": "📦 Product info", "not_found": "🔍 Not found", "refusal": "🚫 Refused"}

st.set_page_config(page_title="NovaMart Assistant", page_icon="🛒", layout="centered")  # browser tab title + layout


@st.cache_data(ttl=60)  # cache the result for 60 s (Streamlit reruns the script often)
def load_shoppers():
    try:
        return requests.get(f"{API}/shoppers", timeout=10).json()  # [{customer_id, name}, ...]
    except Exception:  # API down -> empty list (we show an error below)
        return []


def new_session():  # start a fresh conversation
    st.session_state.session_id = f"ui-{uuid.uuid4().hex[:8]}"  # new memory on the server side
    st.session_state.history = []  # clear the chat on screen


if "session_id" not in st.session_state:  # first page load
    new_session()

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("🛒 NovaMart")
    shoppers = load_shoppers()
    if not shoppers:
        st.error(f"API not reachable at {API}")
        st.stop()  # stop rendering the rest of the page
    labels = {f"{s['customer_id']} · {s['name']}": s["customer_id"] for s in shoppers}  # "C005 · Vikram Rao" -> "C005"
    picked = st.selectbox("Signed in as (demo login)", list(labels), index=4)  # default = 5th shopper (C005)
    cid = labels[picked]  # the customer_id sent with every /chat call
    if st.session_state.get("cid") != cid:  # switching shopper = new session (no shared memory)
        st.session_state.cid = cid
        new_session()
    if st.button("🆕 New conversation", use_container_width=True):
        new_session()
    st.caption(f"session `{st.session_state.session_id}`")
    st.divider()
    st.markdown("**Try:**\n- running shoes for flat feet\n- is the blue one in size 9?\n- I need a laptop\n"  # demo prompts
                "- Mujhe ek sasta Bluetooth speaker chahiye\n- compare the first two\n- Do you sell electric cars?\n"
                "- what did Priya buy?")


def render(reply: dict):  # draw one assistant reply
    st.caption(TYPE_LABEL.get(reply["answer_type"], reply["answer_type"]))  # e.g. "🛍️ Recommendation"
    st.markdown(reply["answer"])  # the answer sentence
    if reply.get("comparison"):  # comparison table rows from the API
        names = [p["name"] for p in reply["products"]]  # one column per product
        table = {"field": [r["field"] for r in reply["comparison"]]}  # first column = field names
        for i, n in enumerate(names):
            table[n] = [str(r["values"][i]) for r in reply["comparison"]]  # i-th value of every row
        st.dataframe(table, hide_index=True, use_container_width=True)
    for p in reply.get("products", []):  # one card per product
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])  # wide left column, narrow right column
            c1.markdown(f"**{p['name']}**  \n`{p['product_id']}` · {p['reason']}")  # name, ID, reason
            c2.markdown(f"### ₹{p['price']:,}")  # big price
            c2.caption("✅ in stock" if p["in_stock"] else "❌ out of stock")
    st.caption(f"trace `{reply['trace_id']}` · [replay]({LINK_API}/trace/{reply['trace_id']})")  # link to the audit record


def send(text: str, mode: str = "text"):  # send one message to the API and store the result
    st.session_state.history.append({"role": "user", "text": text, "mode": mode})  # show the user's message
    try:
        r = requests.post(f"{API}/chat", json={"session_id": st.session_state.session_id, "customer_id": cid,  # identity sent in the BODY
                                               "message": text, "input_mode": mode}, timeout=90)  # LLM can take a while
        r.raise_for_status()  # HTTP error -> exception
        st.session_state.history.append({"role": "assistant", "reply": r.json()})
    except Exception as e:
        st.session_state.history.append({"role": "assistant", "error": f"Request failed: {e}"})  # show the error in the chat


# ---------------------------------------------------------------- chat
st.title("NovaMart Shopping Assistant")
st.caption("Ask in English, Hindi or Hinglish. Type, or use the microphone.")

audio = st.audio_input("🎤 Speak your question (optional)", key=f"mic-{len(st.session_state.history)}")  # new key per turn = mic resets after use
if audio is not None:  # the user recorded something
    with st.spinner("Transcribing…"):
        t = requests.post(f"{API}/transcribe", files={"audio": ("speech.wav", audio.getvalue(), "audio/wav")}, timeout=60)  # speech -> text
    if t.ok and t.json().get("text"):
        with st.spinner("Thinking…"):
            send(t.json()["text"], "voice")  # the SAME /chat pipeline as typing
    else:
        st.warning(f"Could not transcribe: {t.text[:150]}")

prompt = st.chat_input("What are you shopping for?")  # the text box at the bottom
if prompt:  # user pressed Enter
    with st.spinner("Thinking…"):
        send(prompt)

for turn in st.session_state.history:  # draw the whole conversation
    with st.chat_message(turn["role"]):  # user bubble or assistant bubble
        if turn["role"] == "user":
            st.markdown(("🎤 " if turn["mode"] == "voice" else "") + turn["text"])  # mic icon for voice turns
        elif "error" in turn:
            st.error(turn["error"])
        else:
            render(turn["reply"])
