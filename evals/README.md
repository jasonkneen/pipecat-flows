# Pipecat Flows audio evals

A small suite of **audio behavioral evals** that drive the example bots end to end
(speech in → Flows/LLM function-calling → speech out) and assert on what they do.
Run it before a release to confirm the core Flows behaviors still work.

It builds on Pipecat's eval framework (`pipecat eval`, 1.4.0+): the harness
synthesizes each user turn with a local TTS (**Kokoro**), streams it to the bot so
its real VAD/STT/LLM/TTS run, transcribes the bot's audio with a local STT
(**Moonshine**), and scores the replies with a local LLM judge (**Ollama**).

## What's covered

Each scenario targets a distinguishing feature of its example so the suite covers
the core surface without redundancy:

| Bot | Scenario(s) | Core features |
| --- | --- | --- |
| `quickstart/hello_world.py` | `hello_world` | Smoke test: one node transition (edge function), `end_conversation` action |
| `food_ordering.py` | `food_ordering_pizza`, `food_ordering_sushi` | Dynamic routing, edge + node functions, global functions, `function` pre-action, `FlowsFunctionSchema` |
| `food_ordering_direct_functions.py` | `food_ordering_direct` | The `@flows_tool_options` direct-function path |
| `patient_intake.py` | `patient_intake` | Context strategies (RESET, RESET_WITH_SUMMARY), array-of-object args |
| `restaurant_reservation.py` | `restaurant_reservation_available`, `restaurant_reservation_no_availability` | Conditional branching on function results, dynamic node creation |
| `insurance_quote.py` | `insurance_quote` | Parameterized node creation, data flowing through transitions |
| `llm_switching.py` | `llm_switching` | `LLMSwitcher` integration — function calling keeps working after switching providers mid-call |

The `llm_switching` scenario additionally needs **`OPENAI_API_KEY`,
`GOOGLE_API_KEY`, and `ANTHROPIC_API_KEY` all set** (the bot constructs every
provider). It switches only between those three, so AWS Bedrock creds aren't
required. The harness can't see which provider is active, so the scenario
verifies what actually breaks: `switch_llm` fires with the right provider and
tool-calling/responses keep working after each switch.

**Not covered** (don't fit a single-bot audio harness cleanly): `warm_transfer.py`
(Daily + a live human agent) and `multi_worker_handoff.py` (multi-worker
architecture). Worth adding later as specialized evals.

## Prerequisites

1. **Dev dependencies** (provide the bots' STT/TTS/LLM and the `pipecat` CLI):
   ```bash
   uv sync --group dev
   ```
2. **Judge LLM** — a local Ollama with the default judge model:
   ```bash
   ollama pull gemma2:9b
   ```
   To judge with OpenAI instead, add an `eval:` block to
   `scenarios/_judge_audio.yaml` (see the comment in that file).
3. **API keys** in `examples/.env` (the *bots'* services are real; only the user
   TTS, bot STT, and judge are local):
   - `CARTESIA_API_KEY` — bot TTS (all bots) and bot STT for `hello_world`
   - `DEEPGRAM_API_KEY` — bot STT (all bots except `hello_world`)
   - `GOOGLE_API_KEY` — `hello_world`'s LLM
   - The key matching `LLM_PROVIDER` for the other bots (default
     `openai_responses` → `OPENAI_API_KEY`). See `examples/env.example`.
4. **First run** downloads the local Kokoro and Moonshine models — expect a
   one-time delay.

## Running

From the repo root:

```bash
# The whole suite (the release gate). -a records each conversation's audio.
uv run pipecat eval suite evals/manifest.yaml -a
```

The suite spawns each bot on its own port, runs its scenarios (2 at a time by
default — tune `concurrency:` in `manifest.yaml`), prints a pass/fail tally, and
exits non-zero if anything fails. Logs and recordings land in
`evals/eval-runs/<timestamp>/`; inspect `logs/<scenario>.eval.log` to debug a
failure.

### Iterating on a single scenario

Run one bot by hand and drive a single scenario against it — the bot stays up so
you can re-run as you edit:

```bash
# Terminal 1: the bot, headless on the eval transport
uv run examples/food_ordering.py -t eval

# Terminal 2: one scenario, verbose (per-turn / per-expectation lines)
uv run pipecat eval run evals/scenarios/food_ordering_pizza.yaml -v
```

### Cross-provider

The bots pick their LLM from `LLM_PROVIDER` (default `openai_responses`). To
confirm provider-agnostic function formatting, re-run the suite with the env var
set, e.g. `LLM_PROVIDER=anthropic uv run pipecat eval suite evals/manifest.yaml`
(also `google`, `aws`). `hello_world` always uses Google.

## Adding a scenario

1. Drop a `scenarios/<name>.yaml` file in. Start from an existing one — pull in
   the shared audio config with `user: !include _user_audio.yaml` and
   `judge: !include _judge_audio.yaml`.
2. Drive the flow with `user:` turns and assert with `expect:`. Prefer
   `function_call` assertions (the strongest signal that the right Flows handler
   fired with the right args); keep `eval:` judge criteria semantic and lenient
   to limit flakiness. Open with an observe-only turn for the bot's greeting.
3. List the scenario under its bot in `manifest.yaml`. If the bot is new, make it
   eval-capable first by adding an `"eval"` entry to its `transport_params` (see
   any covered example) — `PipelineWorker` handles the RTVI wiring automatically.

The scenario file format (events, judge/user modality, `!include`, etc.) is
documented in `pipecat/evals/scenario.py`.
