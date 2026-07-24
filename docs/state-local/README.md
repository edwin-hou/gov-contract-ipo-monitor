# State and local adapter policy

State/local coverage is explicit, source-by-source, and never represented as nationwide complete.

An adapter is eligible only when it consumes an official government API, RSS/Atom feed, award page, signed contract, notice of award, or approved governing-body minutes. It must extract:

- canonical government source URL;
- award identifier or a document identifier;
- awarded status and award date;
- legal recipient;
- awarding body;
- scope;
- obligation/current value/ceiling semantics when disclosed;
- retrieval/publication timestamps and immutable payload hash.

Rejected sources include bid boards without award records, vendor announcements, scraped news, social posts, and “intent to award” notices that remain protestable or unexecuted.

To add an adapter:

1. implement a collector under `src/contract_ipo_monitor/sources/`;
2. normalize into `ContractEvidence` with `EvidenceClass.B` for official documents or `A` for structured award records;
3. add recorded fixtures and adversarial tests;
4. register the adapter in `AdapterInventory` with its enabled and authoritative status;
5. document cadence, terms, rate limits, geography, and known blind spots.
