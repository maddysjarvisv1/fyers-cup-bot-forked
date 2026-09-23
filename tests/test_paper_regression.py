"""Paper-mode no-regression: the demo lifecycle (arm -> entry -> stop-exit) must
still work end-to-end after the live-risk changes.
"""
from broker import PaperBroker
from bot import CupBot, ScriptedFeed


def test_paper_lifecycle_regression(tmp_path):
    broker = PaperBroker(state_path=str(tmp_path / "paper.json"), reset=True)
    watchlist = [{"symbol": "WINNER", "rim": 100, "trigger": 104, "qty": 100}]
    feed = ScriptedFeed({"WINNER": [99, 104, 93]})  # near-rim -> breakout -> stop-out
    bot = CupBot(broker, feed, watchlist, state_path=str(tmp_path / "st.json"),
                 mode="PAPER", verbose=False)

    bot.cycle()  # arm buy-stop near rim
    assert any(o["side"] == "BUY" for o in broker.open_orders())

    bot.cycle()  # breakout -> entry fill -> pos_state + sell-stop
    assert "WINNER" in bot.pos_state
    assert bot.pos_state["WINNER"]["sell_stop_id"]
    assert any(p["symbol"] == "WINNER" for p in broker.positions())

    bot.cycle()  # drop below SL -> stop-exit -> cleanup
    assert "WINNER" not in bot.pos_state
    assert not any(p["symbol"] == "WINNER" for p in broker.positions())
