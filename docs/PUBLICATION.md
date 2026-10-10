# Publication boundary

This is a fresh, selected source repository, not a dump of a production checkout or its Git history. The source was inventoried against the running deployment. Private source hashes and release inspection records are retained outside this public repository. A public file manifest and validation report bind the submitted tree.

Included: native executed-result and live venue collectors; protocol decoding; identity/order/fork handling; durable journals and compression verification; retained candle/read/reference processing; commit notifications; API and feed delivery; scoped account keys; invoice/receipt verification; usage ledger; chart watches and embeds; chart rendering, studies, drawings, and Pro terminal.

Excluded: bulk market-history downloaders, remote archive acquisition, private deployment scripts, machine addresses, operational credentials, environment files, account databases, customer records, original market datasets, website marketing pages, console frontend, news/community UI, artwork/vendor bundles, compiled binaries and third-party node implementations.

Publication adaptations:

- Replace deployment-only filesystem defaults with local project paths.
- Extract identity helpers from the former community service without publishing that service.
- Exclude Python network-collector classes; retain their record parser and processing contracts.
- Remove REST history acquisition from the live venue collector. Its normalized reader and fixture ingestion are retained.
- Remove site-only news/listing installation from the API entrypoint.
- Retain the loopback read bridge and verified-watch registry; exclude external history federation and the private discovery/acquisition worker. New watch requests remain queued until an operator verifies their market inputs.
- Remove synthetic default chart data; charts require explicit input.
- Host the Pro terminal in a minimal independent shell, with only its required styles and controls.
- Parameterize tests and external interface builds for an isolated Linux checkout.
- Update legacy fixture expectations to current product names/prices, complete receipt evidence, identity rebasing and fail-closed empty-store behavior. No production service behavior is changed by these test adaptations.

No public customer data, server keys or live settlement proof is required to reproduce the offline checks. Fixture identities and test keys have no production authority. Source publication does not grant permission to use production credentials or redistribute upstream datasets.

The repository is a submission-time snapshot. We do not backdate commits or assert that every included line was first authored inside an unverified hackathon eligibility window. The platform's deadline and the owner's applicable participation rules govern that declaration.
