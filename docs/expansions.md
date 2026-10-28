# Expansions

Ideas for taking the pipeline beyond news, recorded 2026-09-16 after the Wan2.2 S2V test held a
31-second talking-head clip without seams or camera moves. None of this is built unless a section
says so. The guiding decision: **one pipeline, not a fork.** The expensive machinery is generic;
news is one way of getting a script.

## What is news-specific and what is not

News-specific: search, ranking, extraction, the evidence context, and the summarize prompt. That is
the "where does the script come from" layer.

Generic presenter-video production: narration from a script, avatar, the template runner with
placeholders and template families (LTX, Wan S2V), segmenting, hero images, compositing, the run
queue with retry and cancel, tenants and keys, the admin UI, migrations, logging.

## The three axes of a run

| Axis | Today | Planned additions |
|---|---|---|
| Script source | news search; provided script (API `script_text`, skips search and script stages) | provided script in the UI; a site or product page as the source; a bullet list the LLM expands |
| Audio source | Chatterbox narration of the script | a provided track (song, voice-over), skipping the audio stage |
| Layout | none; green-panel key with per-segment heroes | side-by-side composite on a plain backdrop; layout as a profile field |

## Promotional videos from a site

Works with the existing news machinery as a profile, not new code:

- Search text `site:example.com` (engines that honor `site:` do; a second phrase without the filter
  pulls outside coverage; one profile has one search string).
- Max age days 0, because blog posts and package pages are mostly undated and the news age rule
  would discard them.
- crawl4ai reads the site cleanly; no bot walls.
- Rank query says what relevant means for the promo ("damore.ai consulting services and packages").
- The profile's own summarize prompt: brand voice, what is offered, a call to action, no
  "According to [source]". Its own hero prompt: product and brand imagery.

Not yet: pulling the site's own images for heroes; following links beyond what the search returns.

## Music videos from a provided track

The Wan S2V template's own showcase is a singer with a guitar; the model was built for singing as
much as speech.

- Provided audio on a run and profile; no narration stage.
- One long chain rather than segments, since a song has no story boundary to cut on. A 3-minute
  track is about 38 segments of 77 frames; memory stayed flat at 65 GB during the 7-segment test,
  time is roughly linear (31 s took on the order of 25 to 30 minutes on the Spark).
- Video prompt describes the performance instead of "talking head"; no heroes.
- To test first, manually: quality over minutes, identity drift, lipsync on sung vowels.

## Segmenting under Wan

Keep segments, change what they mean. Under LTX a segment is "80 words because the model cannot
hold longer than about 10 seconds". Under Wan the cap can be minutes, so the cap becomes a
per-template setting. Segments stay useful as story units: retry granularity, cancel points, one
hero per story rather than one per 80 words; for a promo, a segment is a scene. Stitching costs
nothing when there is one segment.

## Order of work, when the time comes

1. Per-family render cap (LTX about 10 s, Wan minutes). The one change the Wan result forces.
2. Layout as a profile field (none, green-panel key, side-by-side).
3. Provided script in the UI. The promo product with almost no new code.
4. Provided audio track. The music-video product.
5. Only if a second product truly needs the machinery without news: extract the generic services
   into a package then, not before.
