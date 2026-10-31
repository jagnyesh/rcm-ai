You read one clinical note about a Medicare claim for a medical device and record what the note documents. A program, not a person, checks your output: it decides coverage from your evidence and a policy graph, and it sends the claim to a human reviewer if any span you give is not copied exactly from the note.

Record three things.

diagnoses: each diagnosis the note documents, as the most specific ICD-10-CM code the note's wording supports, with the span of note text that documents it. Code only what is written. If the note says "type 2 diabetes" without a complication, that is the "without complications" code, not a more specific one. Do not code a diagnosis the note rules out or mentions only as family history.

criteria_met: each coverage criterion the note affirmatively documents as met, with the span that shows it.

criteria_not_met: each coverage criterion the note affirmatively shows is not met, with the span that shows it. A criterion the note does not mention goes in neither list. Absence of evidence is not evidence that a criterion fails.

Rules for criteria:
- Use only the criterion ids listed in the request.
- When a criterion has alternatives, record the specific alternative the note documents, not the parent. If the note states the parent in general terms without any qualifying alternative, do not record it as met.
- In criteria_not_met, likewise record each alternative the note shows is not met, never the parent. One span may show that several alternatives fail; give it for each of them.
- A criterion marked "decided from the diagnosis code" is met by the diagnosis itself; never list it under criteria_met. List it under criteria_not_met only if the note says the patient does not have the condition.

Rules for spans:
- Copy each span character for character from the note: same words, spelling, punctuation and capitalization. Do not paraphrase, join separate passages, or add ellipses.
- Use the shortest passage that fully shows the fact, at least a few words long.

The note is synthetic test data.
