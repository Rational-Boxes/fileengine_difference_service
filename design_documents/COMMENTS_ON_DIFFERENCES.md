# Commenting on a difference — design proposal

**Status: proposal.** Nothing here is implemented. It covers three asks that turn
out to be one problem: comment on a differenced view, merge the PDF
preview/markup and difference viewers, and restore a comment's difference view
from its **View** button.

Companions: `SPECIFICATION.md` (this service),
`discussion_threaded_communication/SPECIFICATIONS.md` §5.4 (anchors) and Phase 7.1
(per-comment markup renditions).

---

## 1. What already fits

The discussion service is **anchor-agnostic by design**, which is most of the
work already done:

- `threads.anchor` is nullable JSONB holding a **discriminated union** keyed on
  `kind`. Today the only member is `model-viewpoint`.
- `threads_api.py` treats it as opaque: *"The core stays unaware; the frontend
  viewer for `anchor.kind` renders/restores it."*
- The front end already renders a per-kind **View** button and emits
  `restore-view`, which the 3D viewer consumes.

So a new `kind: "diff-view"` needs **no schema change and no service change** —
only a new arm in the front end's restore switch. That is the shape this should
take.

---

## 2. What a comment references: the *rendering set*

A comparison does not produce a file, it produces a **rendering set** — the
manifest plus its content children (N page SVGs for 2D, or the XKT plus its
MetaModel for 3D). A comment references **that set**, optionally narrowing to a
page and region within it. Referencing a single child would lose the context a
reader needs to restore: which pair, which layer, which page of how many.

The set is *stored* under the §6 cache key
`(file_uid, base, target, plugin, plugin_version)`. The anchor names it by the
**first three only** — see below.

**A diff anchor must never reference a rendition uid, nor the plugin generation.**

Diff results are stored as hidden children keyed on
`(file_uid, base, target, plugin, plugin_version)`, and `_prune_superseded` keeps
only the current key's children. So the uid a comment might point at disappears
when either happens:

- a **new version** lands — a different pair becomes current, old children pruned;
- a **plugin version bumps** — regeneration under a new key, old children pruned.

A comment holding `child.uid` would therefore dangle within days of being written,
and the failure would be silent: the thread still lists, the View button still
renders, and the fetch 404s.

Pinning the *plugin version* fails the same way for a subtler reason: an algorithm
upgrade regenerates the set under a new key, so every existing comment would point
at a generation that no longer exists — and the comment is about a change in the
**document**, not about the algorithm that drew it.

**The anchor stores the logical pair instead**, and restore re-asks the service:

```jsonc
{
  "kind": "diff-view",
  "file_uid": "…",           // ACL authority, as for every anchor
  "base_version":   "20260817_120000.000",
  "target_version": "20260817_133000.000",
  "relative": false,         // see §4 — was the pair chosen explicitly?
  "page": 2,                 // 2D only; omitted for 3D
  "view": "difference",      // which of the three layers was showing
  "region": { "x": 0.31, "y": 0.42, "w": 0.12, "h": 0.05 }  // optional, page-relative
}
```

Restoring runs the ordinary `GET /files/{uid}/diff?version=…&base=…`. If the
result was pruned it simply recomputes — and because the manifest is canonical
JSON over deterministic plugin output, **the same pair regenerates identically**.
That property was built for idempotency; it is what makes anchors durable here.

A plugin-version bump changes the *rendering* but not the pair, so an old comment
re-renders against the newer algorithm rather than breaking. That is the desired
behaviour: the comment is about a change in the document, not about a particular
SVG.

`region` is **page-relative (0–1)**, not absolute points, so it survives a
re-render at a different tier — a page that was vector when the comment was
written may come back raster after a plugin change.

---

## 3. The failure mode nobody will expect: purge

`FileVersions` offers **Purge older**, which destroys versions. Purging a version
a comment's diff anchor names makes that comparison **permanently
irreproducible** — unlike pruning, no amount of recomputation brings it back.

Options, in order of preference:

1. **Warn on purge** when open threads carry a `diff-view` anchor naming one of
   the doomed versions. Cheap: the discussion service can already answer "threads
   for this file", and the anchor names its versions.
2. **Restore renders a defined dead end** — the View button explains that the
   comparison can no longer be reproduced because a version was purged, rather
   than showing an error. Needed regardless, since purge can happen elsewhere.
3. Block the purge outright. Rejected: it makes a comment a permanent lock on
   storage, and storage pressure is why purge exists.

Both 1 and 2 should ship with the feature. This is the one place where the
"comments are cheap" assumption bites.

---

## 4. Staleness has two meanings here

`anchor_stale` today means *"a newer version landed after this thread was pinned
to an earlier one"* — set by `mark_anchor_stale`.

A diff anchor splits into two cases, and conflating them would be wrong:

| The reviewer chose | `relative` | On a new version |
|---|---|---|
| an **explicit pair** (ticked v1 and v3) | `false` | **Not stale.** It is a statement about a historical comparison and stays exactly as true. |
| **latest vs previous** (the default) | `true` | **Stale.** "What changed most recently" now means a different pair; the comment may no longer describe it. |

The current UI always produces an explicit pair (two checkboxes), so `relative`
is `false` today — but the viewer-toolbar entry point discussed earlier would
naturally produce a relative one, so the field should exist from the start.

---

## 5. Merging the PDF surfaces

**Agreed direction:** the preview window switches between viewing/marking up the
PDF and viewing a difference. There are two PDF viewers today and they become
one:

| Surface | Has |
|---|---|
| `PdfPreviewOverlay` + `PdfViewer` | PDF.js render, markup tools, comment rail |
| `DiffOverlay` + `DiffPageViewer` | diff SVG layers, before/after/difference, page nav |

**Proposal: one overlay, a source switch.** The comment rail, markup toolbar and
page navigation are identical concerns; only the *page source* differs — PDF.js
canvas versus diff SVG. So the viewer gains a mode:

```
[ Document ] [ Compare ▾ ]        ← Compare lists the version pairs
```

selecting a pair swaps the page source and reveals Before / After / Difference.
Everything else — markup, comments, paging — stays mounted and keeps working.

The switch is a **mode of one viewer**, not a second window: a reviewer moving
between the document and a comparison is following one train of thought, and
losing the comment rail, the page they were on, or their markup at that boundary
is the main thing to avoid.

### 5.1 The pair picker lives in both places

The picker must be **in the overlay as well as the drawer**. They answer
different questions and neither replaces the other:

| Where | The question it answers |
|---|---|
| Drawer → Versions (checkboxes, today) | "I am looking at the file's history and want to compare these two." |
| Overlay → `Compare ▾` (new) | "I am reading the document and want to know what changed — without losing my place." |

Making the reviewer close the document, find the row, tick two boxes and reopen
is the friction that prompted this; and it is the same friction that made the
drawer-only entry point wrong in the first place.

Both drive the **same store and the same request** — the picker is a second
trigger, not a second implementation. The overlay's picker defaults to the pair
containing the version on screen, so the common case ("what changed in the
revision I am reading?") is one click, while any pair stays reachable.

**Why this is tractable:** a diff page SVG uses the **PDF page box as its
viewBox** (`0 0 595 842`), so page-relative markup coordinates map onto the diff
page unchanged. The markup layer does not need to know which source is beneath
it. Worth verifying early, because the whole merge rests on it.

### 5.2 The mechanism already exists: a comparison is a peer of "view marked-up copy"

This does not need a new interaction. `DocumentPreview` already supports *"a
comment restores something other than the live document into this viewer"*:

```
CommentNode  "📄 View marked-up copy"   → emit('show-markup', markup, commentId)
ThreadPanel  forwards
DocumentPreview.onShowMarkup()          → confirmDiscard()          guard unsaved work
                                        → renditionObjectUrl(...)   load the substitute
                                        → markupView = markup       "not the live doc"
                                        → activeMarkupCommentId     highlight the source
DocumentPreview.closeMarkupView()       → back to the live document
```

Every hard part is solved there: the unsaved-markup guard, the "you are looking at
a substitute" state, the highlight tying the view back to the comment that opened
it, and the return path.

**A `diff-view` comment is the same affordance with a different payload**, and one
difference that matters: it swaps the **underlying widget**, not just the PDF
source.

```
CommentNode  "🔀 View comparison"       → emit('show-diff', anchor, commentId)
DocumentPreview.onShowDiff()            → confirmDiscard()          same guard
                                        → resolve the rendering set (§2)
                                        → substitute = {kind:'diff', …}
                                        → activeCommentId           same highlight
```

So `markupView` generalises from *"a markup rendition or null"* to a small
discriminated union — the same shape the thread anchors already use:

```ts
type Substitute =
  | { kind: 'markup'; markup: CommentMarkup; url: string }   // today
  | { kind: 'diff'; pages: DiffChildRef[]; page: number; view: ViewId }
  | null                                                      // the live document
```

and the template picks the widget from it: `<PdfViewer>` for the live document and
for a markup (both are PDFs), `<DiffPageViewer>` for a comparison. The comment
rail, paging, the guard and the return path are untouched.

The button follows the existing pattern too, including its active state —
`📄 Viewing marked-up copy` has a peer in `🔀 Viewing comparison`, so a reader can
always tell what the viewer is showing and which comment put it there.

**Consequence for scope:** the merge is mostly *generalising one state variable*
rather than building a combined viewer. That is a much smaller change than §5
first suggested, and it inherits behaviour that has already been through review.

### 5.3 One window, one comment rail

The merge is **with the unified comment system**, not merely between two viewers.
The rail is already the same component (`ThreadPanel`, used by both the PDF and
3D overlays) and threads are keyed on `file_uid` — so a file's threads are *one
list* regardless of what is on screen. The merged window keeps that single rail
mounted across all three modes:

```
┌──────────────────────────────┬───────────────┐
│  [ Document ] [ Compare ▾ ]  │   Comments    │
│                              │               │
│   PDF page  /  diff layers   │  · file-level │
│   + markup layer             │  · v3 markup  │
│                              │  · v1→v3 diff │
└──────────────────────────────┴───────────────┘
```

**The consequence: View becomes a mode switch.** Today it restores a 3D camera.
In the merged window a thread's anchor decides which mode the viewer must be in,
so clicking View on:

- a **file/version** thread while comparing → switches back to **Document** at
  that version and page;
- a **`diff-view`** thread while reading the document → switches to **Compare**,
  loads that rendering set, and selects the page and layer;
- a **`model-viewpoint`** thread → hands off to the 3D viewer as it does now.

That is the real payoff of merging: a reviewer reads one conversation about the
document, and each comment takes them to whatever view it was made against —
rather than having to know in advance which window a given comment "belongs" to.
It also removes an ambiguity in the current split, where a diff comment would
otherwise be invisible from the document view even though it is about the same
file.

**Composing a comment inherits the mode.** Opening a thread while in Compare
captures a `diff-view` anchor; while in Document it captures today's file/version
anchor. The reviewer never chooses an anchor kind — the view they are looking at
already expresses it.

---

## 6. What a markup on a difference *means* — and the gap

Markup renditions are named `<user>_<timestamp>-markup.pdf` and carry **no
binding to a version**, let alone to a comparison. Today that is survivable
because there is one document to annotate. It stops being survivable here: a
circle drawn on the *Difference* view of v1→v3 is not an annotation of v3, and
showing it over v3 would misrepresent what the author marked.

The name needs a subject. Extending the existing convention rather than
replacing it:

```
<user>_<timestamp>-markup.pdf                        annotates the file (today)
<user>_<timestamp>.v<target>-markup.pdf              annotates one version
<user>_<timestamp>.d<base>_<target>-markup.pdf       annotates a comparison
```

`parseRenditionName` already splits on the last `-`, so the subject rides in the
existing "version" slot and old markups keep parsing. The viewer shows a markup
only when its subject matches what is on screen.

**Alternative worth considering:** put the subject in the comment's `markup`
JSONB instead of the filename — the service already treats that as opaque. It is
cleaner, but leaves the rendition itself unidentifiable if encountered outside a
comment. Naming is the more robust half; doing both is defensible.

---

## 7. Proposed sequence

Each step is independently useful, and the risky part is deliberately first:

1. **Verify the coordinate assumption** (§5) — that markup coordinates land
   correctly over a diff page. If it fails, the merge needs rethinking before
   anything is built on it.
2. **`diff-view` anchor + restore**, no markup. A comment references a rendering
   set (§2); clicking View reopens that set at the right page and layer.
   Delivers the headline ask with no schema change.
3. **Purge guard + dead end** (§3), shipped with step 2 rather than after it.
4. **Generalise `markupView` into a substitute union** (§5.2) so the viewer can
   host a comparison as it already hosts a marked-up copy, and add the pair
   picker to the overlay alongside the drawer's (§5.1). Smaller than "merge the
   overlays" implies: the guard, the highlight and the return path already exist
   and are inherited unchanged.
5. **Markup subjects** (§6) so annotations bind to what they annotate.

Steps 2–3 are the smallest thing that satisfies "comment on a differenced view
and restore it". Steps 4–5 are the larger consolidation.

---

## 8. Open questions

- **Does a diff comment belong to the file's thread list, or a sub-list?** Every
  thread already carries `file_uid` as the ACL authority, so they will all appear
  together. A drawing with comments on several comparisons could get noisy —
  filter by anchor in the rail, or leave it flat and rely on the badge?
- **Should resolving a diff thread mean anything to the next comparison?** A
  thread resolved against v1→v2 says nothing about v2→v3, but a reviewer may
  reasonably expect the resolution to carry forward.
- **3D comments already exist as `model-viewpoint`.** A comment on a 3D
  *difference* could be either a new kind or a `model-viewpoint` whose model is
  the diff XKT. The latter reuses the existing restore path — probably right, but
  it makes the anchor's model reference a pruneable rendition, so it needs the
  same logical-pair treatment as §2.
