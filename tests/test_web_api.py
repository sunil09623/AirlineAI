"""Tests for the offline web API (upload -> review) and its narrative."""

import re
from datetime import date

from fastapi.testclient import TestClient

from airshop.ndc import builder, engine
from airshop.ndc.models import (
    AirShoppingRequest,
    OriginDest,
    Preferences,
    Traveler,
)
from airshop.web.server import _narrative, create_app


def fixture_pair():
    request = AirShoppingRequest(
        transaction_id="TRX-WEB",
        travelers=[Traveler(ptc="ADT", quantity=1)],
        origin_dests=[
            OriginDest(
                origin="LHR",
                destination="DXB",
                departure_date=date(2027, 6, 1),
                cabin="Y",
            )
        ],
        preferences=Preferences(currency="USD", max_connections=2),
    )
    response = engine.shop(request)
    rq_xml = builder.build_air_shopping_rq(request)
    rs_xml = builder.build_air_shopping_rs(response, request)

    # New RS: drop one offer and add a node.
    dropped = response.itineraries[0].itinerary_id
    pattern = re.compile(
        r"\s*<Offer>(?:(?!</Offer>).)*?" + re.escape(dropped) + r"(?:(?!</Offer>).)*?</Offer>",
        re.S,
    )
    new_xml = pattern.sub("", rs_xml, count=1)
    new_xml = new_xml.replace(
        "</Response>",
        "<MarketingMessages><MarketMessage><MessageText>Flash sale</MessageText>"
        "</MarketMessage></MarketingMessages></Response>",
        1,
    )
    return rq_xml, rs_xml, new_xml, dropped


def make_client(tmp_path) -> TestClient:
    return TestClient(create_app(tmp_path))


def upload(client: TestClient, name: str, content: str):
    response = client.post(
        "/api/upload",
        files={"file": (f"{name}.xml", content.encode("utf-8"), "application/xml")},
        data={"name": name},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_health_reports_offline(tmp_path):
    client = make_client(tmp_path)
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["offline"] is True
    assert "chat_available" in body


def test_health_default_model_matches_readme(tmp_path):
    """The default model must be the one the README tells users to pull."""
    from airshop.agent.local import AgentConfig

    assert AgentConfig().model == "qwen2.5:3b-instruct"


def test_chat_unavailable_reports_reason_not_500(tmp_path, monkeypatch):
    """A missing model must yield a usable message, never an unhandled 500."""
    client = make_client(tmp_path)
    monkeypatch.setattr(
        "airshop.web.server._ollama_reachable", lambda _url: True
    )
    monkeypatch.setattr(
        "airshop.web.server._model_available", lambda _m, _u: False
    )
    response = client.post("/api/chat", json={"message": "hi"})
    assert response.status_code == 503
    assert "ollama pull" in response.json()["detail"]


def test_chat_server_down_reports_503(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    monkeypatch.setattr(
        "airshop.web.server._ollama_reachable", lambda _url: False
    )
    response = client.post("/api/chat", json={"message": "hi"})
    assert response.status_code == 503
    assert "not reachable" in response.json()["detail"]


def test_agent_text_extracts_list_content():
    """Regression: SDK message content is a list of TextContent, not a str."""
    from openhands.sdk.llm import Message, TextContent

    from airshop.agent.local import _agent_text

    assert _agent_text(Message(role="assistant", content=[TextContent(text="Hi")])) == "Hi"
    assert _agent_text(Message(role="assistant", content="plain")) == "plain"
    assert (
        _agent_text(
            Message(
                role="assistant",
                content=[TextContent(text="a"), TextContent(text="b")],
            )
        )
        == "a\nb"
    )
    assert _agent_text(Message(role="assistant", content=[])) == ""


def test_model_available_detects_installed_model(tmp_path, monkeypatch):
    import airshop.web.server as server

    class FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def sendall(self, _data):
            return None

        def recv(self, _n):
            if getattr(self, "_sent", False):
                return b""
            self._sent = True
            return (
                b"HTTP/1.1 200 OK\r\n\r\n"
                b'{"models":[{"name":"qwen2.5:3b-instruct"}]}'
            )

    monkeypatch.setattr(server.socket, "create_connection", lambda *a, **k: FakeSock())
    assert server._model_available("qwen2.5:3b-instruct", "http://127.0.0.1:11434")
    assert not server._model_available("qwen2.5:70b", "http://127.0.0.1:11434")


def test_index_serves_ui(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert "NDC AirShopping Reviewer" in response.text


def test_upload_detects_message_kinds(tmp_path):
    rq_xml, rs_xml, _, _ = fixture_pair()
    client = make_client(tmp_path)

    assert upload(client, "baseline_rq", rq_xml)["kind"] == "request"
    assert upload(client, "baseline_rs", rs_xml)["kind"] == "response"


def test_upload_rejects_empty_file(tmp_path):
    client = make_client(tmp_path)
    response = client.post(
        "/api/upload",
        files={"file": ("empty.xml", b"   ", "application/xml")},
        data={"name": "empty"},
    )
    assert response.status_code == 400


def test_review_reports_missing_and_extra(tmp_path):
    rq_xml, rs_xml, new_xml, dropped = fixture_pair()
    client = make_client(tmp_path)
    upload(client, "baseline_rq", rq_xml)
    upload(client, "baseline_rs", rs_xml)
    upload(client, "new_rs", new_xml)

    response = client.post(
        "/api/review",
        json={
            "baseline_rs": "baseline_rs",
            "new_rs": "new_rs",
            "baseline_rq": "baseline_rq",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()

    removed = [e["key"] for e in body["entities_removed"]]
    assert dropped in removed
    assert any("MarketingMessages" in path for path in body["extra_nodes"])
    assert body["identical"] is False
    assert "MISSING" in body["narrative"]
    assert body["baseline_inventory"]["Offer"] > body["new_inventory"]["Offer"]


def test_review_identical_responses(tmp_path):
    rq_xml, rs_xml, _, _ = fixture_pair()
    client = make_client(tmp_path)
    upload(client, "baseline_rs", rs_xml)
    upload(client, "same_rs", rs_xml)

    body = client.post(
        "/api/review", json={"baseline_rs": "baseline_rs", "new_rs": "same_rs"}
    ).json()
    assert body["identical"] is True
    assert "identical" in body["narrative"].lower()


def test_review_unknown_message_returns_404(tmp_path):
    client = make_client(tmp_path)
    response = client.post(
        "/api/review", json={"baseline_rs": "nope", "new_rs": "nope2"}
    )
    assert response.status_code == 404


def test_messages_endpoint_lists_uploads(tmp_path):
    rq_xml, rs_xml, _, _ = fixture_pair()
    client = make_client(tmp_path)
    upload(client, "baseline_rq", rq_xml)
    upload(client, "baseline_rs", rs_xml)

    names = [m["name"] for m in client.get("/api/messages").json()["messages"]]
    assert "baseline_rq" in names and "baseline_rs" in names


def test_message_summary_counts_entities(tmp_path):
    _, rs_xml, _, _ = fixture_pair()
    client = make_client(tmp_path)
    upload(client, "baseline_rs", rs_xml)
    body = client.get("/api/messages/baseline_rs/summary").json()
    assert body["root"] == "AirShoppingRS"
    assert body["entity_counts"]["Offer"] >= 1


def test_narrative_handles_identical_report():
    rq_xml, rs_xml, _, _ = fixture_pair()
    from airshop.ndc.diff import diff_xml, summarize_message

    report = diff_xml(rs_xml, rs_xml)
    review = type(
        "R",
        (),
        {
            "baseline_summary": summarize_message(rs_xml),
            "new_summary": summarize_message(rs_xml),
        },
    )()
    assert "identical" in _narrative(report, review).lower()
    _ = rq_xml
