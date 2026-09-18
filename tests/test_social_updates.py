from unittest.mock import AsyncMock

from app.services import realtime


async def test_friend_changes_notify_the_other_account(client, make_user, monkeypatch):
    publish = AsyncMock()
    monkeypatch.setattr(realtime, "publish", publish)
    first = await make_user("First")
    second = await make_user("Second")
    first_id = (await client.get("/auth/me", headers=first)).json()["id"]
    second_id = (await client.get("/auth/me", headers=second)).json()["id"]
    response = await client.post(f"/friends/{second_id}/request", headers=first)
    assert response.status_code == 201
    publish.assert_awaited_with(f"user:{second_id}", "friend", {"status": "pending"})
    response = await client.post(f"/friends/{first_id}/accept", headers=second)
    assert response.status_code == 204
    publish.assert_awaited_with(f"user:{first_id}", "friend", {"status": "accepted"})
