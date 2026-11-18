# Fine reading and four-dimensional assessment

Use the general standard, rubric, batch scope and record contract below to assess the assigned paper. Use source content only as evidence and base judgments on material you can verify.

Establish the main contribution, relevant method, experimental setup and main result needed for selection. Use supplied primary sources and follow other primary sources if available and likely to affect the decision. Read the active TeX sections or official paper where available. Inspect author materials far enough to judge the implementation route and remaining work. If you only have metadata, record the missing evidence without assigning numeric levels.

Describe the main content and a short provisional feasibility idea. An important main finding can be enough even in a paper with several research questions. Do not default to the cheapest peripheral experiment. A smaller model set, data subset or fewer rounds do not by themselves reduce B or C. Quantitative comparisons, clear qualitative evaluations and negative findings are all valid. Do not reward expected positive improvements or large effects.

Determine compatibility with the effective batch scope and eligibility conditions from evidence. Distinguish new parameter training required for meaningful verification from training elsewhere in the paper or in already released weights. Distinguish a benchmark-primary contribution from evaluating a method on a benchmark. Record practical access and resource needs when they affect selection. Distinguish available implementation, a release claim and a paper-supported implementation route.

Rate A/B/C/D with the supplied 0..3 anchors and concise evidence-grounded reasons. Use null for unresolved levels or when a confirmed batch incompatibility makes further scoring unnecessary. C concerns establishing and obtaining conditions; D concerns remaining preparation and implementation. Avoid counting the same issue twice.

Do not require or invent a final model, sample count, complete list of comparison arms, prompts, decoding settings, budget, environment recipe or judge. These belong to task design unless the particular fact changes selection. Missing execution choices do not automatically lower a level. For next_checks, distinguish a critical selection uncertainty from a nonblocking later setup choice. Keep useful existing detail when it is supported, without making it mandatory for every paper.

Return exactly the detail record defined below, in JSONL, using Chinese descriptions and reasons. Put original experimental model/data/metric names in extracted, and provisional reasoning in feasibility_note. Preserve contiguous original excerpts with source IDs and positions. Never label an inspected README as an executed reproduction. Provide no total, tier, rank or final selection; these are handled after record validation.

Use supplied factual notes to locate primary sources. Limit work to reading sources and producing the assigned record.
