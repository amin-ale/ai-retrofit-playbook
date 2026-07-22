# The AI Retrofit Playbook

Patterns for bolting an LLM feature onto software that already has users, a schema, an
auth model, and a budget.

This is not a greenfield guide. Greenfield is easy: nothing to break, no one to page at
2am, no finance team asking why the OpenAI line item tripled. Retrofit is the hard
version of the job (the one clients actually pay for), and it has its own failure modes
that the "build a chatbot in an afternoon" tutorials never mention.

Everything below is written from the retrofit seat: you inherited the codebase, you don't
get to rewrite it, and the feature has to ship without regressing anything that currently
works.

## How to read this

Seven patterns. Each one is structured the same way:

- **Use it when** gives the situation that makes this the right call. **Skip it when**
  covers the situation where it's over-engineering or actively wrong.
- **The burn** is an archetypal failure. These are composites, not incident reports. No
  real numbers are claimed anywhere in this document; where a threshold appears in code
  it is a configuration value you set, not a measured result.
- **Sketch**: a comment-free code fragment showing the shape of the solution, not a
  drop-in library.
- Each pattern ends on **The judgment call**, the one sentence I'd actually say on a call.

The patterns are ordered roughly by when the decision comes up: architecture first, then
runtime behavior, then the things that stop you from getting fired.

A note on the sketches: they are illustrative and deliberately incomplete. Error paths,
observability, and framework glue are elided so the shape stays visible. Treat them as a
diagram that happens to be executable, not as code to paste.

---

## 1. Sidecar vs in-process

The first fork in the road. Does the AI feature live inside the existing application
process, or does it run as a separate service the app talks to over the network?

### Use in-process when

The feature is small, synchronous, and shares the app's data model closely: a "summarize
this record" button, an autocomplete on a form field. The call is a function call that
happens to hit an API. You get the app's existing auth, database session, feature flags,
and logging for free. Don't build a distributed system to add a summarize button.

### Use a sidecar when

Any one of these is true: the AI work has a wildly different resource profile (a request
that holds a connection open for 40 seconds while streaming does not belong in the same
worker pool as your 20ms CRUD endpoints); you need to deploy and roll back the AI feature
independently of the main app; the AI stack pulls in dependencies you don't want in the
main app's image; or a second team owns the feature and you want a clean contract between
them.

### The burn

Team ships an "ask your data" copilot in-process, inside the same synchronous web workers
that serve the rest of the app. It works in the demo. Then real usage arrives. Each
copilot request pins a worker for the length of a slow model call, and under load the
worker pool saturates on AI requests. Now the login page is slow. The AI feature took down
functionality that had nothing to do with AI, because they shared a thread pool. The fix
was to move the AI work off the request-serving workers. That is a sidecar, arrived at
the expensive way, in production, during an incident.

### Sketch

In-process, when it's genuinely small:

```python
async def summarize_record(record_id: str, user: User) -> str:
    record = await records.get(record_id, tenant=user.tenant_id)
    prompt = build_summary_prompt(record)
    result = await llm.complete(prompt, max_tokens=300, timeout=15)
    return result.text
```

Sidecar, when it's not. The app never imports the model client, it speaks HTTP to a
service it can scale and roll back on its own:

```python
async def summarize_record(record_id: str, user: User) -> str:
    record = await records.get(record_id, tenant=user.tenant_id)
    resp = await ai_sidecar.post(
        "/summarize",
        json={"tenant": user.tenant_id, "payload": record.to_ai_payload()},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["summary"]
```

The important line is `record.to_ai_payload()`: the sidecar boundary is also a data
boundary, and it forces you to decide explicitly what leaves the main app. That is a
feature, not a cost.

### The judgment call

Start in-process. Move to a sidecar the moment the AI call's latency or failure profile
starts threatening endpoints that have nothing to do with AI. Don't do it preemptively;
don't wait for the incident either.

---

## 2. Streaming

Whether the user sees tokens as they're generated or waits for the whole response.

### Use streaming when

The output is long enough that the full-response latency is longer than a user will
patiently stare at a spinner: chat, long summaries, generated documents. Streaming
doesn't make the model faster; it makes the wait feel like progress instead of a hang, and
it lets the user bail early when the answer is obviously going the wrong way.

### Skip streaming when

The output is short (a classification label, a single extracted field, a yes/no). The
plumbing cost of streaming (SSE or websockets through your proxy, load balancer buffering,
partial-render state in the client, harder error handling mid-stream) is real, and for a
200ms response it buys nothing. Also skip it when the consumer is another service that
needs the whole structured object before it can act; streaming a JSON blob to a machine
that will `json.loads` it anyway is pure overhead.

### The burn

Retrofit adds streaming to a chat feature. Works locally. In production the responses
still arrive all at once, in a lump, after the full delay. The model was streaming fine;
the reverse proxy in front of the app was buffering the response body before forwarding
it, because nobody turned buffering off for that route. A whole sprint of "streaming" that
the user never saw, defeated by one line of proxy config. Streaming is an end-to-end
property: the model, the app, every proxy in between, and the client all have to
cooperate, and the one that doesn't wins.

### Sketch

Server side, yielding tokens as an event stream:

```python
async def stream_answer(question: str, ctx: Context):
    async def events():
        async for chunk in llm.stream(build_prompt(question, ctx)):
            yield ServerSentEvent(data=chunk.text)
        yield ServerSentEvent(event="done", data="")
    return EventSourceResponse(events(), headers={"X-Accel-Buffering": "no"})
```

That `X-Accel-Buffering: no` header, plus its equivalents at every proxy hop, is the part
people forget. Test streaming through the real production ingress path, not against the app
directly.

### The judgment call

Stream user-facing prose. Don't stream machine-consumed structured output. And never call
a streaming feature done until you've watched it stream through the actual production
edge, not localhost.

---

## 3. Caching tiers

LLM calls are the slowest and most expensive thing in the request. Caching is the highest-
leverage cost lever you have, and it comes in tiers with very different hit rates and very
different correctness risks.

### The tiers, cheapest to build

- **Exact-match cache.** Key on a hash of the fully-resolved prompt. Trivial to build,
  zero correctness risk, only helps when inputs repeat verbatim. Great for shared
  system-level generations, deterministic classification of repeated inputs, hot documents
  everyone asks about.
- **Normalized-match cache** works like exact-match but canonicalizes the input first (trim,
  lowercase, strip volatile fields) so trivially-different inputs share a key. Higher hit
  rate, small correctness risk if your normalization throws away something that mattered.
- **Semantic cache** embeds the query and, on a near-enough vector neighbor, returns the
  cached answer. Highest hit rate, highest risk: "cancel my subscription" and "don't cancel
  my subscription" are neighbors in embedding space and opposites in meaning.

### Use them when

Always build the exact-match tier; it's nearly free and never wrong. Add normalization when
you can see near-duplicate inputs in your logs. Reach for semantic caching only when volume
is high enough to justify it AND the cost of an occasional wrong-but-plausible cached answer
is low: surfacing suggestions, not authorizing refunds.

### Skip semantic caching when

The output drives an irreversible or high-stakes action, or when answers depend on data
that changes faster than your cache TTL. A cache that returns yesterday's account balance is
a bug with good latency.

### The burn

A semantic cache on a support assistant is tuned for a generous similarity threshold to push
the hit rate up. Two customers ask opposite questions that sit close together in embedding
space. The second customer gets the first customer's answer. Confidently, instantly, and
wrong. The hit-rate dashboard looked fantastic the whole time. Cache metrics measure
whether you served from cache, never whether you should have.

### Sketch

Tiered lookup: cheap and safe first, expensive and risky last, and the semantic tier gated
behind an explicit similarity floor:

```python
async def cached_complete(prompt: str, ctx: Context) -> str:
    exact_key = sha256(prompt)
    if hit := await cache.get(exact_key):
        return hit

    norm_key = sha256(normalize(prompt))
    if hit := await cache.get(norm_key):
        return hit

    if ctx.semantic_cache_allowed:
        vec = await embed(prompt)
        neighbor = await vector_store.nearest(vec)
        if neighbor and neighbor.score >= SEMANTIC_FLOOR:
            return neighbor.answer

    answer = await llm.complete(prompt)
    await cache.set(exact_key, answer, ttl=ctx.ttl)
    return answer
```

`ctx.semantic_cache_allowed` is per-feature, defaulting to `False`. Semantic caching is
opt-in per use case, never a global default.

### The judgment call

Exact-match everywhere, normalized where duplicates show up, semantic almost nowhere,
and never on a path where a confidently-wrong cached answer causes real harm.

---

## 4. Eval-gated deploys

The retrofit differentiator. In a normal app, tests are deterministic and a green suite
means the behavior is locked. LLM output is non-deterministic; a prompt tweak that fixes
one case silently breaks three others, and no unit test catches it because the code didn't
change. The model's behavior did. An eval set is your regression test for behavior you
can't assert exactly.

### Use it when

Any LLM feature you intend to keep changing. And you will keep changing it: the prompt,
the model version, the retrieval, the temperature. Without an eval gate, every one of those
changes is a blind deploy. The eval set turns "I think this prompt is better" into "it
scores the same or better on the cases we care about."

### Skip it when

Genuinely never, for a feature under active iteration. What you can skip is complexity:
your first eval set is a handful of hand-picked input/expected pairs in a file and an
assertion that the pass rate doesn't drop. You do not need an eval framework, a judge
model, or a dashboard to start. You need ten examples and a threshold.

### The burn

Someone "improves" the system prompt to fix a specific complaint. The fix works for that
case. It also quietly changes the format of a field three other flows depended on, and
because there was no eval set, nothing caught it. It surfaced days later as a downstream
parsing failure that took hours to trace back to a prompt edit nobody thought was risky.
The prompt is code. It shipped without a test. That's the whole story.

### Sketch

The eval set is data; the gate is one assertion. Grade with exact match and structural
checks where you can, and only reach for a judge model where you genuinely can't:

```python
@pytest.mark.parametrize("case", load_cases("evals/support_cases.jsonl"))
def test_eval_case(case, model):
    output = run_feature(case["input"], model=model)

    if case["grader"] == "exact":
        assert output.strip() == case["expected"]
    elif case["grader"] == "contains":
        assert all(s in output for s in case["expected"])
    elif case["grader"] == "schema":
        assert validates(output, case["schema"])


def test_eval_pass_rate(eval_results):
    rate = eval_results.passed / eval_results.total
    assert rate >= PASS_FLOOR
```

Wire `test_eval_pass_rate` into CI. A prompt change that drops the pass rate below the
floor fails the build, exactly like a broken unit test, which is what it is.

### The judgment call

If the feature is worth iterating on, it's worth ten eval cases and a pass-rate floor in
CI before the second iteration. Build the eval set the day you ship v1, not the day v1
breaks.

---

## 5. Fallback UX

What the user sees when the model is slow, down, rate-limited, or returns garbage. On a
retrofit this is not optional, because the AI feature sits inside a product that worked
before you got there and must keep working when the model doesn't.

### Use a designed fallback when

Always, for any user-facing AI feature. The only question is what the fallback is. The model
provider will have an outage. You will hit a rate limit at the worst possible time. The
model will occasionally return something unparseable. Each of those is a Tuesday, not an
edge case, and the product's behavior in that moment is a design decision. Make it on
purpose instead of shipping an uncaught exception.

### The spectrum of fallbacks, best to worst

- **Graceful degrade to the pre-AI behavior.** The retrofit ideal: if the AI summary fails,
  show the raw record. The feature was additive; its failure returns the user to the
  perfectly good experience that existed before. This is the single biggest argument for
  keeping the old path alive rather than replacing it.
- **Fall back to a cheaper or alternate model**: slower or dumber but up.
- **Queue and notify.** "We'll have this ready shortly." Fine for non-interactive work.
- **Honest empty state**: "Couldn't generate this right now, try again." Acceptable.
- An uncaught error or a spinner that never resolves: the default if you build nothing, and
  unacceptable.

### Skip it when

Never, but scale the effort to the stakes. A background enrichment job can retry and log. A
checkout-flow copilot needs a real, designed, tested degraded path, because its failure is
in front of a paying customer mid-transaction.

### The burn

A dashboard gets an AI-generated insights panel, retrofitted onto a page that already showed
the raw charts fine. The panel calls the model synchronously on page load with no timeout
and no fallback. Model provider has a slow morning. Now the entire dashboard hangs on the
one optional panel, and users can't see the charts that have nothing to do with AI. The
feature was supposed to be additive; a missing fallback made it subtractive. The old data
was right there the whole time and the code refused to show it.

### Sketch

Time-bounded, with the pre-AI experience as the floor:

```python
async def insights_panel(record_id: str, user: User) -> PanelData:
    base = await load_raw_panel(record_id, user)
    try:
        summary = await asyncio.wait_for(
            ai_sidecar.summarize(record_id, user),
            timeout=SUMMARY_BUDGET_SECONDS,
        )
        return base.with_summary(summary)
    except (asyncio.TimeoutError, AIUnavailable):
        return base.with_notice("AI summary unavailable")
```

`base` is computed first and unconditionally. The AI is strictly additive to something that
already renders. The user never waits on the model to see what they could already see.

### The judgment call

The pre-AI experience is your best fallback. So on a retrofit, don't delete it. Keep the
old path warm and fall back to it. A synchronous, un-timed, fallback-free model call on a
page-load path is a production incident you've merely scheduled for later.

---

## 6. Cost guardrails

LLM spend is usage-metered and unbounded by default. A retrofit adds a variable, per-request
cost to a product whose economics were built without it. Left ungoverned, one loop, one
abusive user, or one runaway prompt turns into a bill nobody approved. Guardrails make the
worst case bounded and known.

### The layers

- **Per-request bound.** Cap `max_tokens` and context size on every single call. A model
  will happily generate to its limit; if you didn't set one, you accepted its default.
- **Per-tenant budget** is a rolling spend ceiling per customer. When they hit it, degrade
  to cached/cheaper/queued: a known, designed state, not a surprise on your invoice.
- A **global circuit breaker** is an account-wide spend rate that, when tripped, sheds the
  feature. This is the difference between a bad day and a bad month.

### Use them when

All three, from day one, for any feature calling a metered API. This is the guardrail
category people postpone until the first scary bill, and the first scary bill is precisely
what these prevent. Retrofitting cost controls after an incident is doing it in the wrong
order.

### Skip it when

You can skip the per-tenant layer if there are no tenants: an internal tool with a known,
small user set. You cannot skip the per-request bound; that one is free and universal.

### The burn

An "auto-improve" feature feeds the model's output back in as input to refine it, looping
until "good enough." A prompt phrasing makes "good enough" never trigger for a certain input
class. The loop runs long, each iteration billable, with no per-request iteration cap and no
budget ceiling. The bill for that feature arrives detached from any single user action,
because it wasn't one action. It was one input multiplied by an unbounded loop. Every layer
of guardrail would have caught it independently. There were zero.

### Sketch

Check the budget before spending, bound the spend, record it after:

```python
async def guarded_complete(prompt: str, user: User, purpose: str) -> str:
    if await spend.tenant_over_budget(user.tenant_id):
        return await degraded_path(prompt, user, purpose)
    if await spend.global_breaker_tripped():
        raise FeatureShed(purpose)

    result = await llm.complete(
        prompt,
        max_tokens=MAX_OUTPUT_TOKENS[purpose],
        timeout=REQUEST_TIMEOUT,
    )
    await spend.record(user.tenant_id, result.usage, purpose)
    return result.text
```

The budget check comes before the spend, the cap rides on every call, and usage is recorded
per tenant and per purpose so the dashboard can tell you which feature is expensive before
finance does.

### The judgment call

Every metered call gets a per-request cap, no exceptions. Add per-tenant budgets the moment
there's more than one tenant, and a global breaker before you ever go multi-tenant in
production. These are cheap to build up front and expensive to add after the invoice.

---

## 7. Vendor-lock hedging

Every LLM feature couples you to a provider: their API shape, their model behavior, their
pricing, their uptime. Total independence is a fantasy that costs more than it's worth, and total
coupling is a risk you took without pricing it. Hedging is buying the specific optionality you
actually need and not paying for the rest.

### Use a hedge when

The switching cost of a provider change is high AND a provider change is plausible: pricing
shifts, a model gets deprecated out from under you (this happens on the provider's schedule,
not yours), an outage forces a failover, or a client mandates a specific vendor. The cheap,
almost-always-right hedge is a thin internal interface for the handful of operations you
actually use (complete, stream, embed) so provider specifics live in one adapter instead
of smeared across the codebase.

### Skip the hedge when

You'd be building a lowest-common-denominator abstraction over features you depend on. If a
provider's tool-use, structured-output, or prompt-caching behavior is load-bearing for your
feature, an interface that pretends all providers are identical will either leak that
provider's semantics anyway or force you to give up the thing that made it work. Don't
abstract away the capability you're specifically there to use.

### The burn

A team abstracts their LLM calls behind a generic interface on day one "to stay portable,"
and in doing so routes everything through the plainest possible completion call (no tool
use, no structured output, no provider-specific features) because those didn't fit the
lowest common denominator. They paid the full cost of an abstraction and got a worse product
for it, hedging against a provider switch that never came. The mirror-image burn is just as
real: the team that inlines one provider's SDK into two hundred call sites, then gets a
deprecation notice with a deadline and spends the quarter on a migration a thin adapter would
have made a one-file change. Both directions hurt. The skill is knowing which one you're
closer to.

### Sketch

A narrow port over the operations you truly use. Not a universal LLM abstraction, just the
seam where a provider swap becomes one new adapter instead of a codebase-wide edit:

```python
class LLMPort(Protocol):
    async def complete(self, prompt: str, max_tokens: int, timeout: float) -> Completion: ...
    async def stream(self, prompt: str) -> AsyncIterator[Chunk]: ...
    async def embed(self, text: str) -> list[float]: ...


class AnthropicAdapter:
    def __init__(self, client: AsyncAnthropic, model: str):
        self._client = client
        self._model = model

    async def complete(self, prompt: str, max_tokens: int, timeout: float) -> Completion:
        msg = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            timeout=timeout,
        )
        return Completion(text=msg.content[0].text, usage=msg.usage)
```

The port names only the operations the app depends on. A new provider is a new adapter class;
nothing above the port changes. And when a provider-specific capability is genuinely
load-bearing, you widen the port deliberately and accept the coupling with your eyes open,
rather than pretending it isn't there.

### The judgment call

Wrap the three or four operations you actually call behind a thin adapter. That hedge is
nearly free and saves the migration quarter. Do not build a universal LLM abstraction, and
do not abstract away a provider capability that is the whole reason the feature works.

---

## The through-line

If there's one idea under all seven patterns, it's this: **on a retrofit, the AI feature is
a guest in a house that was already standing.** The house had users, a schema, an auth model,
a latency profile, and a budget before the model showed up, and every one of those is a
constraint the greenfield tutorials get to ignore and you don't.

So the recurring moves are all about containment. Keep the old path alive so you have a
fallback (§5). Put a boundary around the AI work so its latency and failures don't leak into
everything else (§1). Bound its cost so it can't surprise finance (§6); gate its behavior
so a prompt edit can't silently regress the product (§4). Wrap the provider so one deprecation
notice isn't a quarter of work (§7). None of it is exotic. It's the ordinary discipline of
adding a volatile, expensive, non-deterministic dependency to software that people already
rely on, applied on purpose, before the incident, instead of during it.

That discipline is the entire value of hiring someone who has done this before. The demo is
easy. Shipping it into a running product without breaking the product is the job.
