# Place search and activity assessments

## Implemented locally, not deployed

`GET /places/search` accepts `q`, optional activity UUID/slug, and `live=false`
for catalogue-only lookup. An empty query with an activity requests suggestions
before typing. Free users can select catalogue results; Google fallback requires
Plus. Every valid authenticated request records the user's query, activity,
result count and outcome, including misses, provider errors and paywall responses.
This covers submitted place searches, not every keystroke or offline attempts.

The app passes activity context from Lock and activity-linked chat attachments,
does not suppress repeat requests with a client cache, and retains the selected
place ID when creating a plan. Existing app styling stays unchanged apart from
a search-history notice and Google attribution. Native-device QA is still due.

Catalogue records keep source identity, license, source URL, coordinates and
evidence. Wikidata imports require an explicit Philippines country claim and one
Earth coordinate inside supported bounds. Coordinates describe a general location,
not a verified entrance or meeting point. No AI-generated coordinates.

An operator must review a source before recommendations can use it. An activity
search queues a deduplicated assessment against the source evidence; the worker
uses OpenAI Structured Outputs and withholds uncertain, unsuitable or low-confidence
matches. The model does not establish safety, access rights, opening hours or
current availability. Changed source evidence or activity text invalidates matches.

Google content stays separate. Only place IDs persist indefinitely; autocomplete
labels return directly to the requesting client. Selected Google coordinates
expire after 29 days. The maintenance job clears expired coordinates and legacy
provider display text while preserving IDs and plan references. User-authored plan
and message text remains outside that cleanup and needs a separate provenance review
before claiming complete Google-policy compliance.

## Rollout

1. Back up the database, then apply `alembic upgrade head` with the API release.
2. Configure `OPENAI_API_KEY` on the backend and scheduled job only. The local key
   passed a live assessment using public Rizal Park evidence; production configuration
   has not been changed. `PLACE_ASSESSMENT_MODEL` defaults to `gpt-4o-mini`.
3. Preview source records with `python scripts/import_places.py Q2306339`. This fetches
   Rizal Park without accessing the database. `--apply` imports it as pending;
   `--apply --reviewed` approves the source after operator review. Do not run either
   write mode against production without reviewing the preview. The preview was tested;
   no catalogue records have been imported into production.
4. Schedule `python scripts/assess_places.py --limit 20` hourly with the same database
   and backend environment. Each run makes at most 20 assessment attempts, leases jobs
   atomically, retries failures at most three times, and performs retention cleanup.
   Missing OpenAI configuration leaves queued work pending without making calls.
5. Update the public privacy notice before rollout. Search history has a configurable
   90-day retention period, follows account deletion, and can be removed with authenticated
   `DELETE /places/search-history`. No endpoint exposes another user's history. The AI
   receives public place evidence and activity title/category, not users or raw queries;
   requests set `store=false`.

The starter importer accepts explicit Wikidata IDs. Bulk Philippines coverage,
city/province enrichment, search-driven discovery of new places, community submissions
and an operator review UI remain to build. Search misses record demand; they do not
automatically become verified places. No claim of complete coverage across the activity
catalogue or reduced production spending has been measured.

## RevenueCat setup, 2026-09-18

Requested price: PHP 99/month. No yearly product or trial created.

| Item | Identifier |
| --- | --- |
| Project | `proj4317e069` |
| Plus entitlement | `plus` / `entl67aa7d4ed1` |
| Offering | `default` / `ofrng29211c5743` |
| Monthly package | `$rc_monthly` / `pkgedbc107f16a` |
| Android app | `app268768326a`, package `com.sumnora.gimik` |
| Android product | `gimik_plus:monthly` / `prodd608516f68` |
| iOS app | `app4536d5d546`, bundle `com.sumnora.gimik` |
| iOS product | `gimik_plus_monthly` / `prod27b02c3c10` |

These are RevenueCat catalogue records, not published store subscriptions. Both
products are attached to Plus and the monthly package. Offering metadata records
the intended PHP 99 price; metadata does not set the charge. Google Play and App
Store credentials, actual store products/prices, mobile purchase/restore SDK wiring,
backend entitlement synchronization, and sandbox purchase/refund tests remain pending.
Never put the RevenueCat secret or OpenAI key in an `EXPO_PUBLIC_` variable. Rotate
the previously chat-shared service secrets before launch.

## Source rules

Verification: 142 backend tests and 123 app tests passed; TypeScript and targeted
lint checks passed. The migration passed a disposable SQLite upgrade/downgrade
test and PostgreSQL offline SQL generation. One live OpenAI request assessed public
Wikidata evidence successfully. No production migration, app installation, or real
purchase test ran during this change.

- [Wikidata licensing](https://www.wikidata.org/wiki/Wikidata:Licensing): structured data is CC0.
- [Google Places policies](https://developers.google.com/maps/documentation/places/web-service/policies)
  and [service-specific terms](https://cloud.google.com/maps-platform/terms/maps-service-terms)
  define provider storage and attribution restrictions.
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
  defines the response schema used by the assessor.
- [RevenueCat product API](https://www.revenuecat.com/docs/api-v2/product) distinguishes
  catalogue creation from store product creation.
