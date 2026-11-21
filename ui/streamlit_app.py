"""Streamlit chat UI. It only talks to the API (no business logic here).

HOW: shopper picker (stands in for login) -> chat box or microphone -> POST /chat -> render answer,
product cards, comparison table and the trace id. Voice: st.audio_input -> POST /transcribe -> same /chat.
WHY a thin UI: the API is the contract judges test; the UI is just one client of it.
"""
import os
import uuid

import requests
import streamlit as st

API = os.getenv("API_URL", "http://localhost:8000")
TYPE_LABEL = {"recommendation": "🛍️ Recommendation", "comparison": "⚖️ Comparison", "clarification": "❓ Question",
              "product_fact": "📦 Product info", "not_found": "🔍 Not found", "refusal": "🚫 Refused"}

st.set_page_config(page_title="NovaMart Assistant", page_icon="🛒", layout="centered")


@st.cache_data(ttl=60)
def load_shoppers():
    try:
        return requests.get(f"{API}/shoppers", timeout=10).json()
    except Exception:
        return []


def new_session():
    st.session_state.session_id = f"ui-{uuid.uuid4().hex[:8]}"
    st.session_state.history = []


if "session_id" not in st.session_state:
    new_session()

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("🛒 NovaMart")
    shoppers = load_shoppers()
    if not shoppers:
        st.error(f"API not reachable at {API}")
        st.stop()
    labels = {f"{s['customer_id']} · {s['name']}": s["customer_id"] for s in shoppers}
    picked = st.selectbox("Signed in as (demo login)", list(labels), index=4)
    cid = labels[picked]
    if st.session_state.get("cid") != cid:  # switching shopper = new session (no shared memory)
        st.session_state.cid = cid
        new_session()
    if st.button("🆕 New conversation", use_container_width=True):
        new_session()
    st.caption(f"session `{st.session_state.session_id}`")
    st.divider()
    st.markdown("**Try:**\n- running shoes for flat feet\n- is the blue one in size 9?\n- I need a laptop\n"
                "- Mujhe ek sasta Bluetooth speaker chahiye\n- compare the first two\n- Do you sell electric cars?\n"
                "- what did Priya buy?")


def render(reply: dict):
    st.caption(TYPE_LABEL.get(reply["answer_type"], reply["answer_type"]))
    st.markdown(reply["answer"])
    if reply.get("comparison"):
        names = [p["name"] for p in reply["products"]]
        table = {"field": [r["field"] for r in reply["comparison"]]}
        for i, n in enumerate(names):
            table[n] = [str(r["values"][i]) for r in reply["comparison"]]
        st.dataframe(table, hide_index=True, use_container_width=True)
    for p in reply.get("products", []):
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"**{p['name']}**  \n`{p['product_id']}` · {p['reason']}")
            c2.markdown(f"### ₹{p['price']:,}")
            c2.caption("✅ in stock" if p["in_stock"] else "❌ out of stock")
    st.caption(f"trace `{reply['trace_id']}` · [replay]({API}/trace/{reply['trace_id']})")


def send(text: str, mode: str = "text"):
    st.session_state.history.append({"role": "user", "text": text, "mode": mode})
    try:
        r = requests.post(f"{API}/chat", json={"session_id": st.session_state.session_id, "customer_id": cid,
                                               "message": text, "input_mode": mode}, timeout=90)
        r.raise_for_status()
        st.session_state.history.append({"role": "assistant", "reply": r.json()})
    except Exception as e:
        st.session_state.history.append({"role": "assistant", "error": f"Request failed: {e}"})


# ---------------------------------------------------------------- chat
st.title("NovaMart Shopping Assistant")
st.caption("Ask in English, Hindi or Hinglish. Type, or use the microphone.")

audio = st.audio_input("🎤 Speak your question (optional)", key=f"mic-{len(st.session_state.history)}")
if audio is not None:
    with st.spinner("Transcribing…"):
        t = requests.post(f"{API}/transcribe", files={"audio": ("speech.wav", audio.getvalue(), "audio/wav")}, timeout=60)
    if t.ok and t.json().get("text"):
        with st.spinner("Thinking…"):
            send(t.json()["text"], "voice")
    else:
        st.warning(f"Could not transcribe: {t.text[:150]}")

prompt = st.chat_input("What are you shopping for?")
if prompt:
    with st.spinner("Thinking…"):
        send(prompt)

for turn in st.session_state.history:
    with st.chat_message(turn["role"]):
        if turn["role"] == "user":
            st.markdown(("🎤 " if turn["mode"] == "voice" else "") + turn["text"])
        elif "error" in turn:
            st.error(turn["error"])
        else:
            render(turn["reply"])
