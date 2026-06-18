# Pipecat Flows evals

A small suite of **behavioral evals** that drive the example bots and assert on
what they do — which Flows/LLM functions fire, with which args, and what the bot
says back. Run it before a release to confirm the core Flows behaviors still work.

The suite runs **text-only by default**: the harness sends each user turn as text
(RTVI `send-text`) and judges the bot's LLM response directly. This is the right
fit for Flows — it's a library for **conversation flow and context control**, not
speech I/O, so the evals exercise exactly that (node transitions, function
calling, context strategies) without the variance of a full STT→TTS→STT audio
loop. The same scenarios can also run through the **full audio pipeline** when you
want to exercise a bot's real VAD/STT/TTS end to end (see
[Text vs. audio](#text-vs-audio)).

It builds on Pipecat's eval framework (`pipecat eval`, 1.4.0+). In text mode the
only local dependency is the **Ollama** LLM judge that scores `eval:` criteria;
audio mode additionally uses local **Kokoro** (user TTS) and **Moonshine** (bot
STT).

## What's covered

Each scenario targets a distinguishing feature of its example so the suite covers
the core surface without redundancy:

| Bot | Scenario(s) | Core features |
| --- | --- | --- |
| `quickstart/hello_world.py` | `hello_world` | Smoke test: one node transition (edge function), `end_conversation` action |
| `food_ordering.py` | `food_ordering_pizza` | Dynamic routing, direct functions, a global function (`get_delivery_estimate`), `function` pre-action |
| `food_ordering_advanced_functionschema.py` | `food_ordering_sushi` | `FlowsFunctionSchema` with strict `enum` + numeric `minimum`/`maximum` constraints |
| `patient_intake.py` | `patient_intake` | Context strategies (RESET, RESET_WITH_SUMMARY), array-of-object args |
| `restaurant_reservation.py` | `restaurant_reservation_available`, `restaurant_reservation_no_availability` | Conditional branching on function results, dynamic node creation |
| `insurance_quote.py` | `insurance_quote` | Parameterized node creation, data flowing through transitions |
| `multi_worker_handoff.py` | `multi_worker_handoff` | Pipecat multi-worker handoff: a free-form router worker hands off to a Flows worker and the flow runs to completion |
| `llm_switching.py` | `llm_switching` | `LLMSwitcher` integration — function calling keeps working after switching providers mid-call |

The `llm_switching` scenario additionally needs **`OPENAI_API_KEY`,
`GOOGLE_API_KEY`, and `ANTHROPIC_API_KEY` all set** (the bot constructs every
provider). It switches only between those three, so AWS Bedrock creds aren't
required. The harness can't see which provider is active, so the scenario
verifies what actually breaks: `switch_llm` fires with the right provider and
tool-calling/responses keep working after each switch. It's also the heaviest
scenario (three providers, six turns), so it's the most sensitive to machine load
under high `concurrency:` — lower the concurrency if you see it time out.

**Not covered:** `warm_transfer.py` (Daily + a live human agent) doesn't fit a
single-bot harness cleanly. Worth adding later as a specialized eval.

## Text vs. audio

Modality is set per scenario by the `user:` and `judge:` blocks, which default to
text when omitted (as they are in every scenario here):

- **Text (default):** user turns are sent as `send-text`; the judge reads the
  bot's `llm_response`. No user TTS, no bot STT, the bot skips TTS. Fast,
  deterministic, and focused on Flows behavior.
- **Audio:** add the shared includes to a scenario to drive the bot's real audio
  pipeline — the harness synthesizes each user turn with Kokoro, streams it so the
  bot's VAD/STT/LLM/TTS all run, transcribes the bot's audio with Moonshine, and
  judges the transcript:

  ```yaml
  user: !include _user_audio.yaml      # synthesize user turns (Kokoro)
  judge: !include _judge_audio.yaml    # transcribe bot speech (Moonshine), then judge
  ```

  Audio runs are slower and noisier (cloud STT drops, transcription garbling), so
  keep them for occasional pre-release checks rather than the fast iteration loop.

## Prerequisites

1. **Dev dependencies** (provide the bots' services and the `pipecat` CLI):
   ```bash
   uv sync --group dev
   ```
2. **Judge LLM** — a local Ollama with the default judge model (used for `eval:`
   criteria in both modes):
   ```bash
   ollama pull gemma2:9b
   ```
   To judge with OpenAI instead, add an `eval:` block to a scenario's `judge:`
   config (see the comment in `scenarios/_judge_audio.yaml`).
3. **API keys** in `examples/.env` — the *bots* are real Pipecat bots, so they
   need their service keys to start (see `examples/env.example`):
   - The LLM key matching `LLM_PROVIDER` (default `openai_responses` →
     `OPENAI_API_KEY`); `GOOGLE_API_KEY` for `hello_world`; all three of
     OpenAI/Google/Anthropic for `llm_switching`.
   - `CARTESIA_API_KEY` / `DEEPGRAM_API_KEY` — the bots construct TTS/STT
     services. Text mode doesn't exercise them, but audio mode does.
4. **Audio mode only:** the first audio run downloads the local Kokoro and
   Moonshine models — expect a one-time delay. Text runs need neither.

## Running

From the repo root:

```bash
# The whole suite (the release gate), text-only.
uv run pipecat eval suite evals/manifest.yaml
```

The suite spawns each bot on its own port, runs its scenarios (4 at a time by
default — tune `concurrency:` in `manifest.yaml`), prints a pass/fail tally, and
exits non-zero if anything fails. Logs land in `evals/eval-runs/<timestamp>/`;
inspect `logs/<scenario>.eval.log` to debug a failure. Add `-a` to record each
conversation's audio (only meaningful in audio mode).

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

1. Drop a `scenarios/<name>.yaml` file in. Start from an existing one. Leave the
   `user:`/`judge:` blocks out to run text-only (the default); add
   `user: !include _user_audio.yaml` and `judge: !include _judge_audio.yaml` only
   if the scenario should drive the full audio pipeline.
2. Drive the flow with `user:` turns and assert with `expect:`. On each turn,
   assert the `function_call` (the strongest signal that the right Flows handler
   fired with the right args) and a `response` eval (a semantic, lenient judge
   criterion on the bot's reply). The `response` event also paces the run: it
   makes the harness wait for the bot to finish before sending the next turn, so
   the following turn lands on the freshly-active node instead of racing the
   transition. Open with an observe-only turn for the bot's greeting.
3. **Terminal turns are the exception** — a turn whose function ends the
   conversation (`end_conversation`, `complete_order`, etc.) asserts only the
   function call. The `end_conversation` action tears the pipeline down the
   instant the end node is set, so the farewell is never delivered to the harness
   (see the note in `hello_world.yaml`).
4. List the scenario under its bot in `manifest.yaml`. If the bot is new, make it
   eval-capable first by adding an `"eval"` entry to its `transport_params` (see
   any covered example) — `PipelineWorker` handles the RTVI wiring automatically.

The scenario file format (events, judge/user modality, `!include`, etc.) is
documented in `pipecat/evals/scenario.py`.
