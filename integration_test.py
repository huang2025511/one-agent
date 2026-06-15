"""Comprehensive runtime integration test for /workspace modules."""

import asyncio
import os
import sys
import tempfile
import shutil
import time
import traceback

# Ensure workspace is on the path
sys.path.insert(0, "/workspace")

PASS = 0
FAIL = 0
RESULTS = []


def report(name: str, passed: bool, detail: str = ""):
    global PASS, FAIL
    status = "✅ PASS" if passed else "❌ FAIL"
    if passed:
        PASS += 1
    else:
        FAIL += 1
    msg = f"{status} | {name}"
    if detail and not passed:
        msg += f" — {detail}"
    RESULTS.append(msg)
    print(msg)


# ============================================================
# 1. Test EmbeddingStore
# ============================================================
def test_embedding_store():
    print("\n===== 1. EmbeddingStore =====")
    from memory.embeddings import EmbeddingStore, _cosine_similarity, _vector_to_blob, _blob_to_vector

    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test_emb.db")

    try:
        store = EmbeddingStore(db_path=db_path)

        # Create vectors
        v1 = [1.0, 0.0, 0.0] + [0.0] * 381  # dim 384
        v2 = [0.9, 0.1, 0.0] + [0.0] * 381
        v3 = [0.0, 0.0, 1.0] + [0.0] * 381

        # Store
        store.store("mem1", v1)
        store.store("mem2", v2)
        store.store("mem3", v3)

        # Count
        c = store.count()
        report("EmbeddingStore: store + count", c == 3, f"expected 3, got {c}")

        # Search — v1 should be most similar to query v1
        results = store.search(v1, top_k=3)
        report("EmbeddingStore: search returns 3 results", len(results) == 3, f"got {len(results)}")

        # Cosine similarity ordering
        top_id, top_score = results[0]
        report("EmbeddingStore: top result is mem1 (self-similarity)",
               top_id == "mem1" and top_score > 0.99,
               f"id={top_id}, score={top_score}")

        # Second result should be mem2 (closer than mem3)
        second_id, second_score = results[1]
        report("EmbeddingStore: cosine ordering correct (mem2 before mem3)",
               second_id == "mem2" and second_score > results[2][1],
               f"second={second_id}({second_score}), third={results[2][0]}({results[2][1]})")

        # Delete
        store.delete("mem2")
        c2 = store.count()
        report("EmbeddingStore: delete reduces count", c2 == 2, f"expected 2, got {c2}")

        # Search after delete
        results2 = store.search(v1, top_k=10)
        ids = [r[0] for r in results2]
        report("EmbeddingStore: deleted mem2 not in search results",
               "mem2" not in ids, f"ids={ids}")

        # Cosine similarity helper
        sim = _cosine_similarity([1, 0, 0], [1, 0, 0])
        report("EmbeddingStore: _cosine_similarity identical vectors",
               abs(sim - 1.0) < 1e-6, f"sim={sim}")

        sim2 = _cosine_similarity([1, 0, 0], [0, 1, 0])
        report("EmbeddingStore: _cosine_similarity orthogonal vectors",
               abs(sim2) < 1e-6, f"sim={sim2}")

        # Blob round-trip
        blob = _vector_to_blob([1.0, 2.0, 3.0])
        recovered = _blob_to_vector(blob)
        report("EmbeddingStore: vector blob round-trip",
               all(abs(a - b) < 1e-6 for a, b in zip([1.0, 2.0, 3.0], recovered)),
               f"recovered={recovered}")

        store.close()
    except Exception as e:
        report("EmbeddingStore: exception", False, traceback.format_exc())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================
# 2. Test SessionStore
# ============================================================
def test_session_store():
    print("\n===== 2. SessionStore =====")
    from memory.session_store import SessionStore

    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test_sessions.db")

    try:
        store = SessionStore(db_path=db_path)

        # Create session (no title — will be auto-set from first user message)
        store.create_session("sess1")
        sess = store.get_session("sess1")
        report("SessionStore: create_session", sess is not None and sess["id"] == "sess1",
               f"sess={sess}")

        # Add messages
        store.add_message("sess1", "user", "Hello world", tokens=10)
        store.add_message("sess1", "assistant", "Hi there!", tokens=20)
        store.add_message("sess1", "user", "How are you?", tokens=15)
        store.add_message("sess1", "assistant", "I'm fine, thanks!", tokens=25)

        sess2 = store.get_session("sess1")
        msg_count = len(sess2["messages"])
        report("SessionStore: add_message (4 messages)", msg_count == 4,
               f"got {msg_count} messages")

        # Auto-title from first user message
        report("SessionStore: auto-title from first user message",
               sess2["title"] == "Hello world",
               f"title='{sess2.get('title')}'")

        # Fork at point 2 (should copy first 2 messages)
        fork_id = store.fork_session("sess1", 2)
        report("SessionStore: fork_session returns new id", fork_id is not None,
               f"fork_id={fork_id}")

        forked = store.get_session(fork_id)
        forked_msg_count = len(forked["messages"])
        report("SessionStore: forked session has 2 messages",
               forked_msg_count == 2,
               f"got {forked_msg_count}")

        # Verify forked messages content
        if forked_msg_count >= 2:
            report("SessionStore: forked message 0 content correct",
                   forked["messages"][0]["content"] == "Hello world",
                   f"content='{forked['messages'][0]['content']}'")
            report("SessionStore: forked message 1 content correct",
                   forked["messages"][1]["content"] == "Hi there!",
                   f"content='{forked['messages'][1]['content']}'")

        # Fork at point 0 (empty fork)
        fork_id_0 = store.fork_session("sess1", 0)
        forked_0 = store.get_session(fork_id_0)
        report("SessionStore: fork at point 0 has 0 messages",
               len(forked_0["messages"]) == 0,
               f"got {len(forked_0['messages'])}")

        # Fork at point 4 (full copy)
        fork_id_4 = store.fork_session("sess1", 4)
        forked_4 = store.get_session(fork_id_4)
        report("SessionStore: fork at point 4 has 4 messages",
               len(forked_4["messages"]) == 4,
               f"got {len(forked_4['messages'])}")

        # Fork with invalid point
        fork_bad = store.fork_session("sess1", 99)
        report("SessionStore: fork with invalid point returns None",
               fork_bad is None, f"got {fork_bad}")

        # Fork non-existent session
        fork_nosess = store.fork_session("nonexistent", 0)
        report("SessionStore: fork non-existent session returns None",
               fork_nosess is None, f"got {fork_nosess}")

        # get_session_tree
        tree = store.get_session_tree(fork_id)
        report("SessionStore: get_session_tree has parent_id",
               tree.get("parent_id") == "sess1",
               f"parent_id={tree.get('parent_id')}")

        report("SessionStore: get_session_tree has fork_point",
               tree.get("fork_point") == 2,
               f"fork_point={tree.get('fork_point')}")

        # Check parent has children
        parent_tree = store.get_session_tree("sess1")
        children = parent_tree.get("children", [])
        child_ids = [c["id"] for c in children]
        report("SessionStore: parent tree lists forked children",
               fork_id in child_ids,
               f"children={child_ids}, expected fork_id={fork_id}")

        # List sessions
        all_sessions = store.list_sessions()
        report("SessionStore: list_sessions returns all",
               len(all_sessions) >= 4,  # sess1 + 3 forks
               f"got {len(all_sessions)}")

        # Session count
        count = store.get_session_count()
        report("SessionStore: get_session_count",
               count >= 4, f"got {count}")

        # Delete session
        deleted = store.delete_session("sess1")
        report("SessionStore: delete_session returns True", deleted is True)

        deleted_sess = store.get_session("sess1")
        report("SessionStore: deleted session returns None",
               deleted_sess is None, f"got {deleted_sess}")

        store.close()
    except Exception as e:
        report("SessionStore: exception", False, traceback.format_exc())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================
# 3. Test PythonExecutor
# ============================================================
async def test_python_executor():
    print("\n===== 3. PythonExecutor =====")
    from executors.python_runner import PythonExecutor

    try:
        executor = PythonExecutor()

        # Simple math
        result = await executor.execute("2 + 3")
        report("PythonExecutor: simple math (2+3=5)",
               result["success"] and result["result"] == 5,
               f"success={result['success']}, result={result.get('result')}")

        # Multi-line with print
        code = """
x = 10
y = 20
print(x + y)
"""
        result2 = await executor.execute(code)
        report("PythonExecutor: multi-line with print",
               result2["success"] and "30" in result2["output"],
               f"output='{result2.get('output')}', error='{result2.get('error')}'")

        # Output capture
        code2 = """
print("hello world")
print("second line")
"""
        result3 = await executor.execute(code2)
        report("PythonExecutor: stdout capture",
               result3["success"] and "hello world" in result3["output"] and "second line" in result3["output"],
               f"output='{result3.get('output')}'")

        # Import restriction — os not allowed
        result4 = await executor.execute("import os")
        report("PythonExecutor: import os blocked",
               not result4["success"] and "not allowed" in result4.get("error", ""),
               f"success={result4['success']}, error='{result4.get('error')}'")

        # Import restriction — subprocess not allowed
        result5 = await executor.execute("import subprocess")
        report("PythonExecutor: import subprocess blocked",
               not result5["success"] and "not allowed" in result5.get("error", ""),
               f"error='{result5.get('error')}'")

        # Safe import — math allowed
        result6 = await executor.execute("import math\nprint(math.pi)")
        report("PythonExecutor: import math allowed",
               result6["success"] and "3.14" in result6.get("output", ""),
               f"success={result6['success']}, output='{result6.get('output')}'")

        # Timeout test
        result7 = await executor.execute("import time\ntime.sleep(30)", timeout=1)
        report("PythonExecutor: timeout enforcement",
               not result7["success"] and "timed out" in result7.get("error", "").lower(),
               f"success={result7['success']}, error='{result7.get('error')}'")

        # Syntax error
        result8 = await executor.execute("def foo(")
        report("PythonExecutor: syntax error handled",
               not result8["success"],
               f"success={result8['success']}, error='{result8.get('error')}'")

        # Duration tracking
        report("PythonExecutor: duration_ms tracked",
               result.get("duration_ms", 0) >= 0,
               f"duration_ms={result.get('duration_ms')}")

    except Exception as e:
        report("PythonExecutor: exception", False, traceback.format_exc())


# ============================================================
# 4. Test schema_gen
# ============================================================
def test_schema_gen():
    print("\n===== 4. schema_gen =====")
    from skills.schema_gen import auto_tool_schema

    try:
        # No args function
        def no_args():
            """Do something with no args."""
            pass

        schema1 = auto_tool_schema(no_args)
        report("schema_gen: no args function",
               schema1["function"]["name"] == "no_args"
               and len(schema1["function"]["parameters"]["properties"]) == 0
               and len(schema1["function"]["parameters"]["required"]) == 0,
               f"schema={schema1}")

        # Function with defaults
        def with_defaults(query: str, max_results: int = 10, verbose: bool = False):
            """Search with defaults.

            Args:
                query: Search query
                max_results: Max results
                verbose: Verbose mode
            """
            pass

        schema2 = auto_tool_schema(with_defaults)
        props = schema2["function"]["parameters"]["properties"]
        required = schema2["function"]["parameters"]["required"]
        report("schema_gen: function with defaults — required only has query",
               required == ["query"],
               f"required={required}")
        report("schema_gen: max_results has default 10",
               props.get("max_results", {}).get("default") == 10,
               f"props={props.get('max_results')}")
        report("schema_gen: verbose has default False",
               props.get("verbose", {}).get("default") is False,
               f"props={props.get('verbose')}")
        report("schema_gen: query type is string",
               props.get("query", {}).get("type") == "string",
               f"type={props.get('query', {}).get('type')}")
        report("schema_gen: max_results type is integer",
               props.get("max_results", {}).get("type") == "integer",
               f"type={props.get('max_results', {}).get('type')}")

        # Optional types
        from typing import Optional

        def with_optional(name: str, tag: Optional[str] = None):
            """Function with Optional type.

            Args:
                name: The name
                tag: Optional tag
            """
            pass

        schema3 = auto_tool_schema(with_optional)
        props3 = schema3["function"]["parameters"]["properties"]
        report("schema_gen: Optional[str] maps to string",
               props3.get("tag", {}).get("type") == "string",
               f"type={props3.get('tag', {}).get('type')}")
        report("schema_gen: Optional param with default None has default",
               props3.get("tag", {}).get("default") is None,
               f"default={props3.get('tag', {}).get('default')}")

        # Async function
        async def async_func(x: int, y: float) -> str:
            """Async computation.

            Args:
                x: First value
                y: Second value

            Returns:
                Result string
            """
            return f"{x + y}"

        schema4 = auto_tool_schema(async_func)
        report("schema_gen: async function works",
               schema4["function"]["name"] == "async_func"
               and "x" in schema4["function"]["parameters"]["properties"]
               and "y" in schema4["function"]["parameters"]["properties"],
               f"schema={schema4}")
        report("schema_gen: async required has x and y",
               set(schema4["function"]["parameters"]["required"]) == {"x", "y"},
               f"required={schema4['function']['parameters']['required']}")
        report("schema_gen: float type maps to number",
               schema4["function"]["parameters"]["properties"]["y"]["type"] == "number",
               f"type={schema4['function']['parameters']['properties']['y']['type']}")

        # Description extraction
        report("schema_gen: description extracted from docstring",
               "Search with defaults" in schema2["function"]["description"],
               f"desc={schema2['function']['description']}")

        # Parameter descriptions from docstring
        report("schema_gen: param description from docstring",
               "Search query" in props.get("query", {}).get("description", ""),
               f"desc={props.get('query', {}).get('description')}")

    except Exception as e:
        report("schema_gen: exception", False, traceback.format_exc())


# ============================================================
# 5. Test MCPClient
# ============================================================
async def test_mcp_client():
    print("\n===== 5. MCPClient =====")
    from skills.mcp_client import MCPClient

    try:
        client = MCPClient()

        # list_tools returns empty initially
        tools = client.list_tools()
        report("MCPClient: list_tools returns empty initially",
               tools == [], f"tools={tools}")

        # add_server handles connection failure gracefully
        success = await client.add_server("test", "http://localhost:99999")
        report("MCPClient: add_server handles connection failure",
               success is False, f"success={success}")

        # Server should not be in servers dict after failure
        report("MCPClient: failed server not in servers dict",
               "test" not in client.servers,
               f"servers={list(client.servers.keys())}")

        # list_tools still empty after failed connection
        tools2 = client.list_tools()
        report("MCPClient: list_tools still empty after failed add",
               tools2 == [], f"tools={tools2}")

        # close_all on empty client
        await client.close_all()
        report("MCPClient: close_all on empty client works", True)

    except Exception as e:
        report("MCPClient: exception", False, traceback.format_exc())


# ============================================================
# 6. Test api.dashboard
# ============================================================
def test_dashboard():
    print("\n===== 6. api.dashboard =====")
    from api.dashboard import get_dashboard_html

    try:
        html = get_dashboard_html()

        # Valid HTML
        report("dashboard: returns non-empty string",
               isinstance(html, str) and len(html) > 100,
               f"len={len(html)}")

        report("dashboard: starts with DOCTYPE",
               html.strip().startswith("<!DOCTYPE html>"),
               f"starts with: {html[:30]}")

        report("dashboard: contains <html> tag",
               "<html" in html, "")

        report("dashboard: contains </html> tag",
               "</html>" in html, "")

        # Required JS functions
        required_functions = [
            "forkSession",
            "updateSessions",
            "updateCosts",
            "updateStats",
            "updateApprovals",
        ]
        for func_name in required_functions:
            report(f"dashboard: contains JS function '{func_name}'",
                   f"function {func_name}" in html or f"async function {func_name}" in html,
                   f"function not found in HTML")

        # HTML structure
        report("dashboard: contains <script> tag",
               "<script>" in html, "")

        report("dashboard: contains </script> tag",
               "</script>" in html, "")

        report("dashboard: contains <style> tag",
               "<style>" in html, "")

    except Exception as e:
        report("dashboard: exception", False, traceback.format_exc())


# ============================================================
# 7. Test LongTermMemory
# ============================================================
def test_long_term_memory():
    print("\n===== 7. LongTermMemory =====")
    from memory import LongTermMemory

    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test_lt.sqlite")

    try:
        ltm = LongTermMemory(path=db_path, decay_enabled=False)

        # Add entries
        ltm.add("Python is a programming language", source="test", tags="lang")
        ltm.add("JavaScript is used for web development", source="test", tags="lang")
        ltm.add("The weather today is sunny", source="test", tags="weather")

        # Stats
        stats = ltm.stats()
        report("LongTermMemory: stats shows 3 rows",
               stats["rows"] == 3, f"stats={stats}")

        # Search — FTS5
        results = ltm.search("Python", limit=5)
        report("LongTermMemory: search finds Python entry",
               len(results) >= 1, f"got {len(results)} results")

        if results:
            report("LongTermMemory: search result contains Python",
                   "Python" in results[0].get("content", ""),
                   f"content='{results[0].get('content')}'")

        # Search with no match
        results_none = ltm.search("nonexistentxyz", limit=5)
        # FTS5 may return empty or fallback
        report("LongTermMemory: search with no match returns empty or few",
               len(results_none) == 0 or all("nonexistentxyz" not in r.get("content", "") for r in results_none),
               f"got {len(results_none)} results")

        # get_by_id
        entry = ltm.get_by_id("1")
        report("LongTermMemory: get_by_id(1) returns entry",
               entry is not None and entry.get("id") == "1",
               f"entry={entry}")

        if entry:
            report("LongTermMemory: get_by_id content correct",
                   "Python" in entry.get("content", "") or "JavaScript" in entry.get("content", "") or "weather" in entry.get("content", ""),
                   f"content='{entry.get('content')}'")

        # get_by_id non-existent
        entry_bad = ltm.get_by_id("9999")
        report("LongTermMemory: get_by_id(9999) returns None",
               entry_bad is None, f"got {entry_bad}")

        # Paginate
        page = ltm.paginate(page=1, page_size=2)
        report("LongTermMemory: paginate returns items",
               len(page.get("items", [])) == 2,
               f"items={len(page.get('items', []))}")
        report("LongTermMemory: paginate total is 3",
               page.get("total") == 3, f"total={page.get('total')}")

        ltm.close()
    except Exception as e:
        report("LongTermMemory: exception", False, traceback.format_exc())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================
# 8. Test CostTracker
# ============================================================
def test_cost_tracker():
    print("\n===== 8. CostTracker =====")
    from models.cost_tracker import CostTracker

    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test_costs.db")

    try:
        tracker = CostTracker(db_path=db_path, daily_budget=1.0, monthly_budget=20.0)

        # Record costs
        cost1 = tracker.record("openai", "gpt-4", tokens_prompt=100, tokens_completion=50)
        report("CostTracker: record returns cost >= 0",
               cost1 >= 0, f"cost={cost1}")

        cost2 = tracker.record("anthropic", "claude-3", tokens_prompt=200, tokens_completion=100)
        report("CostTracker: second record returns cost >= 0",
               cost2 >= 0, f"cost={cost2}")

        # Total cost
        total = tracker.total_cost()
        report("CostTracker: total_cost > 0",
               total > 0, f"total={total}")

        # Total tokens
        tokens = tracker.total_tokens()
        report("CostTracker: total_tokens = 450",
               tokens == 450, f"tokens={tokens}")

        # Daily cost
        daily = tracker.daily_cost()
        report("CostTracker: daily_cost has cost field",
               "cost" in daily and daily["cost"] > 0,
               f"daily={daily}")

        report("CostTracker: daily_cost has budget field",
               daily.get("budget") == 1.0,
               f"budget={daily.get('budget')}")

        report("CostTracker: daily_cost has remaining field",
               "remaining" in daily,
               f"remaining={daily.get('remaining')}")

        # Monthly cost
        monthly = tracker.monthly_cost()
        report("CostTracker: monthly_cost works",
               monthly["cost"] > 0 and monthly["budget"] == 20.0,
               f"monthly={monthly}")

        # Budget not exceeded
        report("CostTracker: budget not exceeded initially",
               not daily["exceeded"],
               f"exceeded={daily['exceeded']}")

        # Record large cost to exceed budget
        tracker.record("openai", "gpt-4", tokens_prompt=100000, tokens_completion=100000)
        daily2 = tracker.daily_cost()
        report("CostTracker: budget exceeded after large cost",
               daily2["exceeded"] is True,
               f"exceeded={daily2['exceeded']}, cost={daily2['cost']}")

        # check_budget
        budget_check = tracker.check_budget()
        report("CostTracker: check_budget returns overall_exceeded",
               "overall_exceeded" in budget_check and budget_check["overall_exceeded"] is True,
               f"budget_check={budget_check}")

        # by_provider
        by_prov = tracker.by_provider()
        report("CostTracker: by_provider has openai",
               "openai" in by_prov, f"providers={list(by_prov.keys())}")

        # by_model
        by_mod = tracker.by_model()
        report("CostTracker: by_model has gpt-4",
               "gpt-4" in by_mod, f"models={list(by_mod.keys())}")

        # get_recent
        recent = tracker.get_recent(limit=10)
        report("CostTracker: get_recent returns entries",
               len(recent) >= 2, f"got {len(recent)}")

        # Zero tokens
        cost_zero = tracker.record("test", "test-model", tokens_prompt=0, tokens_completion=0)
        report("CostTracker: zero tokens = zero cost",
               cost_zero == 0.0, f"cost={cost_zero}")

    except Exception as e:
        report("CostTracker: exception", False, traceback.format_exc())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================
# 9. Test KnowledgeGraph
# ============================================================
def test_knowledge_graph():
    print("\n===== 9. KnowledgeGraph =====")
    from memory.knowledge_graph import KnowledgeGraph

    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test_kg.db")

    try:
        kg = KnowledgeGraph(db_path=db_path)

        # Add entities
        id1 = kg.add_entity("Python", etype="language", source="test")
        report("KnowledgeGraph: add_entity returns id",
               id1 is not None and id1 > 0, f"id={id1}")

        id2 = kg.add_entity("JavaScript", etype="language", source="test")
        id3 = kg.add_entity("React", etype="framework", source="test")

        # Add duplicate entity — should return same id
        id1_dup = kg.add_entity("Python", etype="language", source="test")
        report("KnowledgeGraph: duplicate entity returns same id",
               id1_dup == id1, f"id1={id1}, dup={id1_dup}")

        # Add relations
        rel_ok = kg.add_relation("Python", "used_for", "web development", source="test")
        report("KnowledgeGraph: add_relation returns True", rel_ok is True)

        kg.add_relation("React", "based_on", "JavaScript", source="test")

        # Search
        results = kg.search("Python", limit=10)
        report("KnowledgeGraph: search finds Python",
               len(results) >= 1 and results[0]["name"] == "Python",
               f"results={results}")

        results_js = kg.search("Java", limit=10)
        report("KnowledgeGraph: search 'Java' finds JavaScript",
               len(results_js) >= 1,
               f"results={results_js}")

        # Query entity
        entity = kg.query_entity("Python")
        report("KnowledgeGraph: query_entity returns entity",
               entity is not None and entity["name"] == "Python",
               f"entity={entity}")

        report("KnowledgeGraph: entity has outgoing relations",
               len(entity.get("outgoing", [])) >= 1,
               f"outgoing={entity.get('outgoing')}")

        # Query non-existent entity
        entity_none = kg.query_entity("NonExistent")
        report("KnowledgeGraph: query non-existent returns None",
               entity_none is None, f"got {entity_none}")

        # Get neighbors
        neighbors = kg.get_neighbors("React", depth=1)
        report("KnowledgeGraph: get_neighbors returns results",
               len(neighbors) >= 1, f"neighbors={neighbors}")

        # extract_from_text
        text = "John Smith works at Google in California. He uses Python and JavaScript."
        count = kg.extract_from_text(text, source="test")
        report("KnowledgeGraph: extract_from_text extracts entities",
               count > 0, f"extracted {count} entities")

        # Verify extracted entities exist
        john = kg.query_entity("John Smith")
        report("KnowledgeGraph: extracted 'John Smith' exists",
               john is not None, f"john={john}")

        google = kg.query_entity("Google")
        report("KnowledgeGraph: extracted 'Google' exists",
               google is not None, f"google={google}")

        # Stats
        stats = kg.stats()
        report("KnowledgeGraph: stats has entities count",
               stats["entities"] > 0, f"stats={stats}")
        report("KnowledgeGraph: stats has relations count",
               stats["relations"] > 0, f"stats={stats}")

        kg.close()
    except Exception as e:
        report("KnowledgeGraph: exception", False, traceback.format_exc())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================
# Run all tests
# ============================================================
async def main():
    test_embedding_store()
    test_session_store()
    await test_python_executor()
    test_schema_gen()
    await test_mcp_client()
    test_dashboard()
    test_long_term_memory()
    test_cost_tracker()
    test_knowledge_graph()

    print("\n" + "=" * 60)
    print(f"TOTAL: {PASS + FAIL} tests — {PASS} passed, {FAIL} failed")
    print("=" * 60)

    if FAIL > 0:
        print("\nFailed tests:")
        for r in RESULTS:
            if "FAIL" in r:
                print(f"  {r}")

    return FAIL


if __name__ == "__main__":
    failures = asyncio.run(main())
    sys.exit(failures)
