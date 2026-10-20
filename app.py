import streamlit as st
from text2sql import answer

st.set_page_config(page_title="Marketing Q&A", layout="wide")
st.title("Marketing metrics assistant")
st.caption("Answers come from certified marketing tables. Always check the SQL and assumptions.")

q = st.chat_input("e.g. What was ROAS by channel last month?")
if q:
    st.chat_message("user").write(q)
    with st.chat_message("assistant"):
        with st.spinner("Working..."):
            r = answer(q)
        if r["status"] == "clarify":
            st.info(r["question"])
        elif r["status"] == "ok":
            st.dataframe(r["data"], use_container_width=True)
            st.caption(f"Rows: {len(r['data'])} | Bytes billed: {r['bytes_billed']:,}")
            with st.expander("Assumptions"):
                for a in r["assumptions"]:
                    st.write(f"- {a}")
            with st.expander("SQL"):
                st.code(r["sql"], language="sql")
        else:
            st.error(f"Could not produce a safe, valid query. Last error: {r['error']}")
