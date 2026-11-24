"""Streamlit UI — design B: chat on the left, live results panel on the right.

It only talks to the API (no business logic here): POST /chat, /transcribe, GET /products/{id}, /trace/{id}, /shoppers, /health.
"""
import os  # read API_URL / PUBLIC_API_URL
import time  # measure response time
import uuid  # random session IDs

import requests  # HTTP calls to the FastAPI backend
import streamlit as st  # the UI framework (re-runs this script on every click / message)

API = os.getenv("API_URL", "http://localhost:8000")  # where the UI calls the API
LINK_API = os.getenv("PUBLIC_API_URL", API)  # what the browser can open (differs in Docker)
LABEL = {"recommendation": "Recommendation", "comparison": "Comparison", "clarification": "Question",  # answer_type -> badge text
         "product_fact": "Product info", "not_found": "Not found", "refusal": "Refused"}
TONE = {"recommendation": "ac", "comparison": "ac", "clarification": "wa", "product_fact": "ok", "not_found": "mu", "refusal": "er"}  # badge colour
CHIPS = ["Running shoes for flat feet", "Mujhe ek sasta Bluetooth speaker chahiye", "I need a laptop",  # one-click demo questions
         "Compare the first two", "Is the blue one in size 9?", "Do you sell electric cars?"]

st.set_page_config(page_title="NovaMart Assistant", page_icon="🛒", layout="wide")  # wide = room for two columns
# Design system adapted from HookForge (dark, cinematic): near-black base, amber accent, Inter / Instrument Serif / JetBrains Mono
st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500&display=swap');
:root{--bg:#08090c;--bg2:#0d0f14;--panel:#12141a;--panel2:#191c24;--line:#23272f;--line2:#2f3440;--text:#ecedf1;--muted:#9aa1ae;
--dim:#646b78;--accent:#f2a93b;--accent2:#ffcf7a;--ok:#34d399;--warn:#fbbf24;--bad:#f87171;--info:#60a5fa}
.stApp{background:radial-gradient(1200px 600px at 70% -200px,rgba(242,169,59,.07),transparent 60%),var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif}
[data-testid="stSidebar"]{background:var(--bg2);border-right:1px solid var(--line)}
[data-testid="stHeader"]{background:rgba(8,9,12,.86);backdrop-filter:blur(6px)}
.block-container{padding-top:1.4rem;max-width:1400px}
h1,h2,h3,h4{font-family:'Instrument Serif',Georgia,serif!important;font-weight:400!important;letter-spacing:.2px}
.hero{background:radial-gradient(700px 300px at 85% 0%,rgba(242,169,59,.16),transparent 70%),linear-gradient(160deg,#151821,#0b0c10);
border:1px solid var(--line);border-radius:16px;padding:18px 24px;margin-bottom:14px}
.eyebrow{font-size:11.5px;text-transform:uppercase;letter-spacing:.14em;color:var(--accent);font-weight:600}
.hero h1{font-size:40px;margin:2px 0 2px;color:var(--text)}.hero p{color:var(--muted);margin:0;font-size:14px}
.stButton>button{background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--text);font-size:13px}
.stButton>button:hover{border-color:var(--accent);color:var(--accent2)}
[data-testid="stChatMessage"]{background:var(--panel);border:1px solid var(--line);border-radius:14px}
[data-testid="stChatInput"] textarea{font-family:Inter,sans-serif}
[data-testid="stExpander"]{border:1px solid var(--line);border-radius:14px;background:var(--panel)}
.badge{display:inline-block;font-size:11.5px;padding:1px 9px;border-radius:999px;margin:0 4px 4px 0;font-weight:500;border:1px solid}
.ac{color:var(--accent);border-color:rgba(242,169,59,.45);background:rgba(242,169,59,.08)}
.ok{color:var(--ok);border-color:rgba(52,211,153,.4);background:rgba(52,211,153,.08)}
.wa{color:var(--warn);border-color:rgba(251,191,36,.4);background:rgba(251,191,36,.08)}
.er{color:var(--bad);border-color:rgba(248,113,113,.4);background:rgba(248,113,113,.08)}
.mu{color:var(--muted);border-color:var(--line2);background:var(--panel2)}
.pcard{background:linear-gradient(180deg,#14161d,#0f1116);border:1px solid var(--line);border-radius:14px;padding:12px 16px;margin-bottom:12px;transition:all .15s}
.pcard:hover{border-color:#4a4030;transform:translateY(-1px)}
.pname{font-weight:600;font-size:15px;color:var(--text)}.pmeta{color:var(--dim);font-size:12px;font-family:'JetBrains Mono',monospace}
.price{font-family:'Instrument Serif',serif;font-size:28px;color:var(--accent2);margin:2px 0 4px}
.reason{font-size:13px;color:var(--muted);margin-top:6px;padding-left:10px;border-left:2px solid var(--accent)}
.filt{font-size:12.5px;color:var(--muted);font-family:'JetBrains Mono',monospace;margin-bottom:10px}
.panel-title{font-size:11.5px;text-transform:uppercase;letter-spacing:.14em;color:var(--muted);font-weight:600;margin:4px 0 8px}
.hero h1{letter-spacing:-.02em;line-height:1.05}
/* signature: every card SETTLES in (staggered), then a "price checked" seal STAMPS onto it */
@keyframes settle{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@keyframes stamp{0%{opacity:0;transform:scale(1.8) rotate(-14deg)}60%{opacity:1;transform:scale(.92) rotate(-8deg)}100%{opacity:1;transform:scale(1) rotate(-8deg)}}
@keyframes breathe{0%,100%{opacity:.55}50%{opacity:1}}
.pcard{position:relative;animation:settle .55s cubic-bezier(.2,.7,.2,1) both}
.stamp{position:absolute;top:12px;right:14px;font:500 10.5px 'JetBrains Mono',monospace;letter-spacing:.08em;text-transform:uppercase;
color:var(--ok);border:1.5px solid rgba(52,211,153,.6);border-radius:6px;padding:2px 7px;animation:stamp .5s cubic-bezier(.2,.7,.2,1) both}
.hero{position:relative;overflow:hidden}
.hero::after{content:"";position:absolute;inset:0;pointer-events:none;background:radial-gradient(500px 220px at 88% 0%,rgba(242,169,59,.12),transparent 70%);animation:breathe 6s ease-in-out infinite}
/* states: every control answers (hover lifts, press settles, keyboard focus is an amber ring) */
.stButton>button{transition:transform .18s cubic-bezier(.2,.7,.2,1),border-color .18s}
.stButton>button:hover{transform:translateY(-1px)}.stButton>button:active{transform:translateY(1px) scale(.98)}
.stButton>button:focus-visible,[data-testid="stChatInput"] textarea:focus-visible,select:focus-visible{outline:2px solid var(--accent)!important;outline-offset:2px}
/* reduced motion: everything visible, nothing moves */
@media (prefers-reduced-motion: reduce){.pcard,.stamp,.hero::after,.stButton>button{animation:none!important;transition:none!important;transform:none!important}}
</style>""", unsafe_allow_html=True)  # HookForge-style CSS + awwwards craft: settle/stamp signature, idle glow, states, reduced motion


@st.cache_data(ttl=60)  # cache: Streamlit reruns often
def load_shoppers():  # [{customer_id, name}] for the demo login
    try:
        return requests.get(f"{API}/shoppers", timeout=10).json()
    except Exception:  # API down -> empty list (we show an error)
        return []


@st.cache_data(ttl=30, show_spinner=False)  # no "Running product(...)" flicker
def product(pid: str) -> dict:  # full product (brand, rating, stock, specs) for the cards
    try:
        return requests.get(f"{API}/products/{pid}", timeout=10).json()
    except Exception:
        return {}


def api_ok() -> bool:  # green / red status light
    try:
        return requests.get(f"{API}/health", timeout=3).json().get("status") == "ok"
    except Exception:
        return False


def new_session():  # fresh conversation = fresh memory on the server
    st.session_state.session_id = f"ui-{uuid.uuid4().hex[:8]}"
    st.session_state.history = []


if "session_id" not in st.session_state:  # first page load
    new_session()

# ---------------------------------------------------------------- sidebar: login, status, how it works
with st.sidebar:
    st.markdown("### 🛒 NovaMart")
    shoppers = load_shoppers()
    if not shoppers:
        st.error(f"API not reachable at {API}")
        st.stop()  # nothing works without the API
    labels = {f"{s['customer_id']} · {s['name']}": s["customer_id"] for s in shoppers}  # "C005 · Vikram Rao" -> "C005"
    cid = labels[st.selectbox("Signed in as (demo login)", list(labels), index=4)]  # sent as customer_id with every message
    if st.session_state.get("cid") != cid:  # switching shopper = new session (no shared memory)
        st.session_state.cid = cid
        new_session()
    if st.button("New conversation", use_container_width=True):
        new_session()
    st.markdown(("🟢 API online" if api_ok() else "🔴 API offline") + f" · session `{st.session_state.session_id}`")
    st.divider()
    st.markdown("**How it works**\n\n1. **Understand:** LLM turns words into filters\n2. **Route:** code picks a path\n"
                "3. **Retrieve + filter:** FAISS + BM25, then SQL\n4. **Write:** LLM, facts only\n5. **Check:** every ID and ₹ verified")


def send(text: str, mode: str = "text"):  # call /chat, then fetch the trace for the side panel
    st.session_state.history.append({"role": "user", "text": text, "mode": mode})
    t0 = time.time()
    try:
        r = requests.post(f"{API}/chat", json={"session_id": st.session_state.session_id, "customer_id": cid,  # identity in the BODY
                                               "message": text, "input_mode": mode}, timeout=90)
        r.raise_for_status()
        reply = r.json()
        trace = requests.get(f"{API}/trace/{reply['trace_id']}", timeout=10).json()  # how the answer was made
        st.session_state.history.append({"role": "assistant", "reply": reply, "trace": trace, "secs": time.time() - t0})
    except Exception as e:
        st.session_state.history.append({"role": "assistant", "error": f"Request failed: {e}"})


def badge(text: str, tone: str) -> str:  # small coloured pill
    return f'<span class="badge {tone}">{text}</span>'


# ---------------------------------------------------------------- layout: header + two columns
st.markdown('<div class="hero"><div class="eyebrow">NovaMart · AI shopping assistant</div>'  # cinematic hero header
            '<h1>Find the right thing, <i>faster</i></h1>'
            '<p>Ask in English, Hindi or Hinglish. Type, tap a suggestion, or speak. Every price and stock level is checked before you see it.</p></div>',
            unsafe_allow_html=True)
left, right = st.columns([1.15, 1], gap="large")  # chat a bit wider than the panel

with left:
    chat_box = st.container(height=470)  # scrollable chat area (filled below, after handling input)
    st.caption("Try:")
    chip_cols = st.columns(3)
    clicked = None
    for i, chip in enumerate(CHIPS):  # one-click demo questions
        if chip_cols[i % 3].button(chip, key=f"chip{i}", use_container_width=True):
            clicked = chip
    audio = st.audio_input("🎤 Speak your question", key=f"mic-{len(st.session_state.history)}")  # new key per turn resets the mic

prompt = st.chat_input("What are you shopping for?")  # text box pinned at the bottom
if audio is not None:  # voice: speech -> text -> the same /chat
    t = requests.post(f"{API}/transcribe", files={"audio": ("speech.wav", audio.getvalue(), "audio/wav")}, timeout=60)
    if t.ok and t.json().get("text"):
        with st.spinner("Thinking…"):
            send(t.json()["text"], "voice")
    else:
        left.warning("🎤 " + (t.json().get("detail") if t.headers.get("content-type", "").startswith("application/json") else t.text[:150]))
elif prompt or clicked:  # typed message or a suggestion chip
    with st.spinner("Thinking…"):
        send(prompt or clicked)

with chat_box:  # draw the conversation
    if not st.session_state.history:
        st.info("Tell me what you're shopping for. For example: *a gift for my father who likes trekking, under ₹3,000*.")
    for turn in st.session_state.history:
        with st.chat_message(turn["role"]):
            if turn["role"] == "user":
                st.markdown(("🎤 " if turn["mode"] == "voice" else "") + turn["text"])
            elif "error" in turn:
                st.error(turn["error"])
            else:
                r = turn["reply"]
                st.markdown(badge(LABEL.get(r["answer_type"], r["answer_type"]), TONE.get(r["answer_type"], "mu"))
                            + f'<span class="pmeta">{turn["secs"]:.1f} s</span>', unsafe_allow_html=True)
                st.markdown(r["answer"])
                if r["products"]:
                    st.caption(f"{len(r['products'])} product(s) → see the results panel")

# ---------------------------------------------------------------- right: live results panel (latest reply)
with right:
    last = next((t for t in reversed(st.session_state.history) if t["role"] == "assistant" and "reply" in t), None)
    st.markdown('<div class="panel-title">Results · live</div>', unsafe_allow_html=True)  # eyebrow-style panel title
    if not last:
        st.caption("Products, filters and the answer trace will appear here.")
    else:
        r, trace = last["reply"], last["trace"]
        search = next((t for t in trace.get("tools", []) if t.get("tool") == "search_products"), None)
        if search:  # active filters chips (from memory)
            f = search.get("filters", {})
            parts = [f.get("product_type") or f.get("category"), f"under ₹{f['budget_max']:,}" if f.get("budget_max") else None,
                     ", ".join(f.get("brands") or []) or None, f.get("color"), f"size {f['size']}" if f.get("size") else None, f.get("use_case")]
            st.markdown('<div class="filt">🔎 ' + " · ".join(p for p in parts if p) + "</div>", unsafe_allow_html=True)
        if r.get("comparison"):  # side-by-side table
            names = [p["name"] for p in r["products"]]
            table = {"field": [row["field"] for row in r["comparison"]]}
            for i, n in enumerate(names):
                table[n] = [str(row["values"][i]) for row in r["comparison"]]
            st.dataframe(table, hide_index=True, use_container_width=True)
        for n, p in enumerate(r["products"]):  # one card per product (n = stagger index)
            full = product(p["product_id"])
            stock = full.get("stock", 0)
            stock_badge = badge("In stock", "ok") if 2 < stock else badge(f"Only {stock} left", "wa") if stock else badge("Out of stock", "er")
            rating = badge(f"{full['rating']} ★", "ac") if full.get("rating") else ""
            st.markdown(f'<div class="pcard" style="animation-delay:{n * 70}ms"><span class="stamp" style="animation-delay:{n * 70 + 380}ms">✓ price checked</span>'
                        f'<div class="pname">{p["name"]}</div>'
                        f'<div class="pmeta">{p["product_id"]} · {full.get("brand") or "no brand"} · {full.get("category", "").replace("_", " ")}</div>'
                        f'<div class="price">₹{p["price"]:,}</div>{stock_badge}{rating}'
                        f'<div class="reason">💡 {p["reason"]}</div></div>', unsafe_allow_html=True)
        with st.expander("How this answer was made"):  # R10: the trace, readable
            steps = [f"{s['step']}: {s.get('source') or s.get('writer')}" + (f" ({s['reason']})" if s.get("reason") else "") for s in trace.get("llm", [])]
            st.markdown(f"**Route:** `{trace.get('route')}` · **Language:** `{trace.get('extraction', {}).get('language')}` · "
                        f"**Time:** {trace.get('latency_ms', 0)} ms")
            st.markdown("**LLM steps:** " + (" · ".join(steps) or "none"))
            if trace.get("retrieved"):
                st.markdown("**Retrieved (before filters):** " + ", ".join(trace["retrieved"][:10]))
            if trace.get("injection_flags"):
                st.markdown("**Injection attempt flagged:** " + ", ".join(trace["injection_flags"]))
            st.caption(f"[Full trace JSON]({LINK_API}/trace/{trace.get('trace_id')})")
