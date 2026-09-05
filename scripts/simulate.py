"""Drive the whole product loop with several people at once, against a live stack.

The unit tests answer "does this endpoint behave"; this answers "does the app
work". It signs up a small cast, gives each of them a different taste, and lets
them swipe, befriend, invite, talk, lock a plan, do it, and write it up - in the
order a real evening actually happens, over HTTP, against the running API with
Postgres and MinIO behind it.

It exists because the interesting failures are not in any single endpoint. They
are in the joins: a keep that is invisible to a friend because visibility was
written but never read back, a feed that keeps offering a card someone already
decided on, an upload presigned against a hostname the phone cannot resolve.
None of those show up in a unit test with storage faked out.

Assertions are on behaviour, and each one prints what it checked, so a failure
says which promise broke rather than which line raised.

    python scripts/simulate.py                     # against http://localhost:8100
    python scripts/simulate.py --base-url http://...

Sign-up is rate limited, correctly, so a second run inside the same window is
turned away with a 429. That is the limiter working rather than a bug; clear the
window first when re-running locally:

    docker exec gora-backend-v2-redis-1 redis-cli FLUSHALL
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

# A minimal valid WebP (a 1x1 pixel). Enough to prove the presign/PUT/GET round
# trip without shipping a fixture file next to this script.
PIXEL_WEBP = bytes.fromhex(
    "524946461a000000574542505650382010000000"
    "3001009d012a0100010002003425a4000370"
)

PASSWORD = "simulate-me-1234"  # noqa: S105 - a throwaway for seeded accounts


@dataclass
class Person:
    """One simulated account, and the taste that decides how they swipe."""

    name: str
    city: str
    # Categories this person keeps. Everything else they pass, which is what
    # gives the feed something to learn from.
    likes: set[str]
    interests: list[str] = field(default_factory=list)
    token: str = ""
    user_id: str = ""
    handle: str = ""
    kept: list[dict] = field(default_factory=list)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def cast() -> list[Person]:
    return [
        Person("Bea Simulated", "Manila", {"Food & Drink", "Culture & Heritage"}, ["slow-mornings"]),
        Person("Caloy Simulated", "Manila", {"Adventure & Outdoors", "Water & Islands"}, ["beach-day"]),
        Person("Dana Simulated", "Manila", {"Food & Drink", "City & Social"}, ["nature-walks"]),
        Person("Elias Simulated", "Cebu", {"Wellness & Sports"}, []),
    ]


class Sim:
    def __init__(self, base_url: str) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=30.0)
        self.checks = 0
        self.failures: list[str] = []

    # -- plumbing ---------------------------------------------------------
    def ok(self, label: str, condition: bool, detail: str = "") -> None:
        self.checks += 1
        if condition:
            print("  ok   " + label)
        else:
            print("  FAIL " + label + (" - " + detail if detail else ""))
            self.failures.append(label)

    def call(
        self,
        method: str,
        path: str,
        person: Person | None = None,
        expect: int | None = None,
        **kw: Any,
    ) -> httpx.Response:
        headers = dict(person.headers) if person else {}
        headers.update(kw.pop("headers", {}))
        response = self.http.request(method, path, headers=headers, **kw)
        if expect is not None and response.status_code != expect:
            raise AssertionError(
                f"{method} {path} -> {response.status_code} (wanted {expect}): {response.text[:300]}"
            )
        return response

    # -- the loop ---------------------------------------------------------
    def sign_up(self, person: Person) -> None:
        suffix = uuid.uuid4().hex[:8]
        # Not a .test/.invalid TLD: the API validates addresses properly and
        # rejects reserved names, which is correct and worth not working around.
        email = f"sim-{person.name.split()[0].lower()}-{suffix}@sim.gora.ph"
        body = {"email": email, "password": PASSWORD, "name": person.name, "gender": "woman"}
        response = self.call("POST", "/auth/register", json=body, expect=201)
        person.token = response.json()["access_token"]
        me = self.call(
            "PATCH",
            "/auth/me",
            person,
            expect=200,
            json={"city": person.city, "interests": person.interests},
        ).json()
        person.user_id, person.handle = me["id"], me["handle"]

    def swipe(self, person: Person, rounds: int = 3, per_round: int = 12) -> dict[str, int]:
        """Work the deck the way a person does: keep what they like, pass the rest."""
        seen: set[str] = set()
        repeats = kept = passed = 0
        for _ in range(rounds):
            page = self.call("GET", "/feed", person, expect=200, params={"limit": per_round}).json()
            for item in page["items"]:
                someday = item["someday"]
                if someday["id"] in seen:
                    repeats += 1
                seen.add(someday["id"])
                if someday["category"] in person.likes:
                    self.call(
                        "PUT",
                        f"/somedays/{someday['id']}/keep",
                        person,
                        expect=201,
                        json={"open_to": ["friends", "everyone"], "party_as": ["duo", "group"]},
                    )
                    person.kept.append(someday)
                    kept += 1
                else:
                    self.call("PUT", f"/somedays/{someday['id']}/pass", person, expect=204)
                    passed += 1
        return {"kept": kept, "passed": passed, "repeats": repeats, "seen": len(seen)}

    def befriend(self, a: Person, b: Person) -> None:
        self.call("POST", f"/friends/{b.user_id}/request", a, expect=201)
        self.call("POST", f"/friends/{a.user_id}/accept", b, expect=204)

    def run(self) -> int:
        people = cast()

        print("\n=== 1. four people sign up ===")
        for person in people:
            self.sign_up(person)
            print(f"  {person.name:<18} @{person.handle:<16} {person.city}")

        print("\n=== 2. everyone works the deck ===")
        for person in people:
            row = self.swipe(person)
            print(
                f"  {person.name:<18} kept {row['kept']:>3}  passed {row['passed']:>3}  "
                f"saw {row['seen']:>3} distinct"
            )
            self.ok(
                f"{person.name} was never shown the same card twice",
                row["repeats"] == 0,
                f"{row['repeats']} repeats",
            )
            self.ok(f"{person.name} kept something", row["kept"] > 0)

        print("\n=== 3. the deck learned from the swiping ===")
        for person in people:
            page = self.call("GET", "/feed", person, expect=200, params={"limit": 20}).json()
            liked = sum(1 for i in page["items"] if i["someday"]["category"] in person.likes)
            share = liked / max(1, len(page["items"]))
            print(
                f"  {person.name:<18} {liked:>2}/{len(page['items']):<2} now in a category "
                f"they keep ({share:.0%})"
            )
            self.ok(
                f"{person.name}'s deck leans toward their taste",
                share > 1 / 9,
                f"{share:.0%} is no better than picking a category at random",
            )

        print("\n=== 4. nothing already decided comes back ===")
        for person in people:
            page = self.call("GET", "/feed", person, expect=200, params={"limit": 30}).json()
            mine = {
                k["someday"]["id"]
                for k in self.call("GET", "/me/somedays", person, expect=200).json()
            }
            offered = {i["someday"]["id"] for i in page["items"]}
            self.ok(f"{person.name} is not re-offered a keep", not (mine & offered))

        print("\n=== 5. friendships, then who can see what ===")
        bea, caloy, _dana, elias = people
        self.befriend(bea, caloy)
        print(f"  {bea.name} <-> {caloy.name}; {elias.name} stays a stranger in another city")

        shared = next((s for s in bea.kept if s["category"] in caloy.likes), None) or bea.kept[0]
        self.call(
            "PUT",
            f"/somedays/{shared['id']}/keep",
            caloy,
            expect=201,
            json={"open_to": ["friends"], "party_as": ["duo"]},
        )
        holders = self.call("GET", f"/somedays/{shared['id']}/holders", bea, expect=200).json()
        self.ok(
            "a friend's keep is visible to their friend",
            any(h["id"] == caloy.user_id for h in holders),
            f"holders were {[h['name'] for h in holders]}",
        )
        strangers = self.call("GET", f"/somedays/{shared['id']}/holders", elias, expect=200).json()
        self.ok(
            "a friends-only keep stays hidden from a stranger in another city",
            not any(h["id"] == caloy.user_id for h in strangers),
        )

        print("\n=== 6. an invite becomes a conversation ===")
        invite = self.call(
            "POST",
            "/invites",
            caloy,
            expect=201,
            json={"to_user_id": bea.user_id, "someday_id": shared["id"], "party": "duo"},
        ).json()
        inbox = self.call("GET", "/invites", bea, expect=200).json()
        self.ok("the invite reaches the right inbox", any(i["id"] == invite["id"] for i in inbox))
        accepted = self.call("POST", f"/invites/{invite['id']}/accept", bea, expect=200).json()
        thread_id = accepted["thread_id"]
        print(f"  {caloy.name} invited {bea.name} to \"{shared['title']}\" - accepted, thread opened")

        self.call(
            "POST", f"/threads/{thread_id}/messages", bea, expect=201, json={"body": "Saturday?"}
        )
        self.call(
            "POST",
            f"/threads/{thread_id}/messages",
            caloy,
            expect=201,
            json={"body": "Saturday works."},
        )
        detail = self.call("GET", f"/threads/{thread_id}", bea, expect=200).json()
        self.ok("both sides of the conversation are in the thread", len(detail["messages"]) >= 2)

        uninvited = self.call("GET", f"/threads/{thread_id}", elias)
        self.ok(
            "someone not in the thread cannot read it",
            uninvited.status_code in (403, 404),
            f"got {uninvited.status_code}",
        )

        print("\n=== 7. locking it in, doing it ===")
        plan = self.call(
            "POST",
            "/plans",
            bea,
            expect=201,
            json={
                "someday_id": shared["id"],
                "date": "2026-10-04",
                "time_slot": "Evening",
                "place": "Toyo Eatery - Chino Roces Ave",
                "split": "kkb",
                "people": [caloy.user_id],
            },
        ).json()
        print(f"  plan for {plan['date']} at {plan['place']}")
        self.call("POST", f"/plans/{plan['id']}/done", bea, expect=200)

        print("\n=== 8. a photo goes to MinIO and comes back ===")
        presigned = self.call(
            "POST",
            "/media/presign",
            bea,
            expect=200,
            json={"filename": "memory.webp", "content_type": "image/webp"},
        ).json()
        put = httpx.put(
            presigned["upload_url"],
            content=PIXEL_WEBP,
            headers={"Content-Type": "image/webp"},
            timeout=30,
        )
        self.ok(
            "the presigned PUT is accepted by the object store",
            put.status_code in (200, 204),
            str(put.status_code),
        )
        photo = self.call(
            "POST", "/media/photos", bea, expect=201, json={"key": presigned["key"]}
        ).json()
        fetched = httpx.get(photo["url"], timeout=30)
        self.ok("the photo reads back through its presigned URL", fetched.status_code == 200)
        self.ok("and it is the bytes that were uploaded", fetched.content == PIXEL_WEBP)

        print("\n=== 9. artwork is public, private media is not ===")
        page = self.call("GET", "/catalog", expect=200, params={"limit": 1}).json()
        art = page["items"][0]["artwork_url"]
        self.ok(
            "catalog artwork loads with no signature", httpx.get(art, timeout=30).status_code == 200
        )
        bare = photo["url"].split("?")[0]
        self.ok(
            "private media is refused without its signature",
            httpx.get(bare, timeout=30).status_code in (400, 401, 403, 404),
        )

        print("\n=== 10. the catalog pages ===")
        first = self.call("GET", "/catalog", expect=200, params={"limit": 40}).json()
        second = self.call(
            "GET", "/catalog", expect=200, params={"limit": 40, "offset": first["next_offset"]}
        ).json()
        overlap = {i["id"] for i in first["items"]} & {i["id"] for i in second["items"]}
        self.ok("consecutive pages do not overlap", not overlap, f"{len(overlap)} shared rows")
        self.ok("the catalog reports its real size", first["total"] > 2000, str(first["total"]))

        print("\n" + "=" * 62)
        if self.failures:
            print(f"FAILED {len(self.failures)} of {self.checks} checks:")
            for name in self.failures:
                print("  - " + name)
            return 1
        print(f"All {self.checks} checks passed.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8100")
    args = parser.parse_args()

    sim = Sim(args.base_url)
    try:
        health = sim.http.get("/health").json()
    except Exception as error:  # noqa: BLE001 - a clear message beats a traceback
        print(f"cannot reach {args.base_url}: {error}")
        return 1
    if health.get("status") != "ok":
        print(f"the API is up but unhealthy: {health}")
        return 1
    return sim.run()


if __name__ == "__main__":
    raise SystemExit(main())
