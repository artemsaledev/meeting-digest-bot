# Telegram AI workflow for the knowledge base

## Target Scenario

The main user interface for the knowledge base is Telegram, not Notion and not
raw CLI commands.

The user should be able to open the Telegram group, mention the bot, and provide
either text or voice input. The bot should understand the intended action,
collect context from the RAG knowledge base, and return a ready-to-use answer,
proposal, instruction, specification, or export artifact.

Notion remains available as a review and manual editing surface, but it is not
the primary interaction model.

## User Intent Classes

The Telegram bot should classify each knowledge-base request into one of these
intent classes:

- `ask_question`: answer how a feature, system, process, or prior decision works.
- `generate_instruction`: create a user-facing instruction from accumulated
  knowledge.
- `generate_spec`: create a technical specification, implementation brief, or
  acceptance criteria.
- `review_proposals`: show proposed changes generated from meetings or manual
  edits.
- `revise_knowledge`: accept a natural-language correction and create a revision
  proposal for canonical knowledge objects.
- `export_bundle`: prepare materials for NotebookLM, Notion, or another external
  AI/RAG tool.
- `health`: show operational status of the knowledge base.

Existing commands like `kb ask`, `kb proposals`, `kb diff`, `kb approve`,
`kb reject`, and `kb apply` remain available, but the preferred UX is guided
buttons plus natural language.

## Preferred Telegram UX

When the user mentions the bot in the group, the bot should show action buttons:

- `Ask`
- `Instruction`
- `Spec`
- `Proposals`
- `Export`
- `Health`

After an action is selected, the bot should process the latest text or voice
message as the user request.

For voice messages:

1. Download Telegram voice/audio file.
2. Transcribe it through the configured external AI token/provider.
3. Normalize the transcript into a clear request.
4. Classify intent.
5. Run the corresponding knowledge-base workflow.
6. Reply in Telegram with a structured result.

For text messages:

1. Normalize the text.
2. Classify intent.
3. Run the corresponding workflow.
4. Reply with a structured result.

## Output Expectations

For questions, the bot should return:

- concise answer;
- key details grouped by system/functionality;
- citations to knowledge objects/chunks when available;
- missing context if the base is insufficient.

For instructions, the bot should return:

- title;
- prerequisites;
- step-by-step flow;
- expected result;
- edge cases;
- source references.

For technical specs, the bot should return:

- context;
- scope;
- functional requirements;
- acceptance criteria;
- integrations/data/contracts;
- risks and open questions;
- source references.

For export requests, the bot should either:

- attach the generated zip/markdown/json file to Telegram; or
- return a short message with the exact file path and what to upload to
  NotebookLM, Notion, or another AI tool.

For revision/correction requests, the bot should:

1. Find the most relevant knowledge objects through RAG.
2. Explain which objects it wants to revise.
3. Create a proposal, not a direct write.
4. Show a readable summary/diff in Telegram.
5. Offer approve/reject/apply buttons.
6. Rebuild indexes/RAG/export after apply.

## AI Orchestrator Prompt

Use this prompt for the AI layer that receives the user's Telegram text or
transcribed voice input.

```text
You are the Knowledge Base Telegram Orchestrator for a company task knowledge
system.

Your job is to turn a user's Telegram text or transcribed voice message into one
clear knowledge-base action.

The knowledge base contains accumulated task discussions and demos, especially
items tagged #task_discussion and #task_demo. It stores canonical task cases,
systems, features, instructions, source bundles, machine bundles, RAG chunks,
and revision proposals.

Primary user experience:
- The user should not need to know internal CLI commands.
- The user can ask in plain language.
- The user can ask by voice.
- Telegram buttons may provide the intended action.
- Notion is secondary; use it only as a review/edit surface when needed.

Classify the request into exactly one intent:
- ask_question
- generate_instruction
- generate_spec
- review_proposals
- revise_knowledge
- export_bundle
- health
- unclear

Return a compact JSON object:
{
  "intent": "...",
  "query": "normalized user request",
  "system": "bitrix|aicallorder|meeting_digest_bot|knowledge_base|unknown",
  "feature_area": "short feature slug or unknown",
  "answer_mode": "general|user_instruction|technical_spec|support_answer",
  "requires_rag": true,
  "requires_export": false,
  "requires_revision_proposal": false,
  "needs_confirmation": false,
  "telegram_reply_style": "answer|document|proposal|status|clarifying_question",
  "clarifying_question": ""
}

Rules:
- If the user asks how something works, use ask_question and answer_mode=general.
- If the user asks to make an instruction, guide, checklist, or user-facing
  steps, use generate_instruction and answer_mode=user_instruction.
- If the user asks for a technical task, ТЗ, spec, implementation plan, or
  acceptance criteria, use generate_spec and answer_mode=technical_spec.
- If the user wants NotebookLM, external AI, archive, bundle, source files, or
  materials to upload elsewhere, use export_bundle.
- If the user corrects existing knowledge or says that an instruction is wrong,
  use revise_knowledge and create a proposal first.
- Never directly mutate canonical JSON from a user correction without proposal
  review.
- Prefer demo evidence over discussion evidence when there is conflict.
- If context is insufficient, ask one clarifying question instead of guessing.
- Keep Telegram replies concise and structured.
```

## Telegram Button Flow

Recommended first implementation:

1. User writes or records a message.
2. Bot shows buttons:
   - `Ask`
   - `Instruction`
   - `Spec`
   - `Export`
   - `Proposals`
3. User taps a button.
4. Bot runs the matching workflow with the latest message as input.
5. Bot returns result and source references.

Recommended second implementation:

1. Bot auto-classifies intent.
2. If confidence is high, it answers directly.
3. If confidence is medium, it asks for button confirmation.
4. If confidence is low, it asks one clarifying question.

## Implementation Notes

The current command layer can stay as the internal backend:

- `kb ask` maps to `ask_question`.
- `rag-knowledge --answer-mode user_instruction` maps to
  `generate_instruction`.
- `rag-knowledge --answer-mode technical_spec` maps to `generate_spec`.
- `export-external-knowledge` maps to `export_bundle`.
- `kb proposals`, `kb diff`, `kb approve`, `kb reject`, `kb apply` map to the
  proposal lifecycle.
- `kb health` maps to `health`.

Audio support should reuse the existing external AI token/provider used for
transcription and summarization. The bot should store only the transcript and
result metadata needed for traceability.

## Product Principle

The knowledge base should feel like a working memory in Telegram:

- ask naturally;
- receive a structured answer;
- generate documents on demand;
- approve corrections without opening Notion;
- use Notion only when visual editing is genuinely useful.
