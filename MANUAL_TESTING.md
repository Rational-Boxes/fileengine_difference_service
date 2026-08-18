# Manual testing — version comparison

A by-hand pass over the whole feature, using the sample files in `samples/`.

These are the **same fixtures the automated tests run against**, so this is not a
parallel demo that can drift — it is the same corpus, driven through the real UI.
The automated suite proves the service *says* the right thing; this pass is for
judging whether it *looks* right, which is the part a test cannot check.

The sample files are **committed** in `samples/`, so this pass can be repeated by
anyone without regenerating anything. If you ever need to rebuild them (after
changing a fixture, say):

```bash
cd difference_service
python3 tools/export_samples.py
```

`src/tests/test_samples_current.py` checks the committed files still match the
fixtures they came from, so a stale sample cannot quietly outlive its fixture and
send a future reviewer chasing behaviour that no longer exists.

---

## 0. Before you start

Everything below assumes the dev stack, the tunnels and `difference_service` are
running. Check in one go:

```bash
# services
for p in 50051:core 8090:http_bridge 3000:frontend 8100:difference; do
  port=${p%%:*}; name=${p##*:}
  printf "  %-12s :%-6s " "$name" "$port"
  (timeout 1 bash -c "</dev/tcp/127.0.0.1/$port" 2>/dev/null && echo UP) || echo DOWN
done

# the service itself
curl -s localhost:8100/readyz     # expect {"status":"ok","checks":{"core":true,"ldap":true}}
```

If anything is down:

```bash
./scripts/start_backend_services.sh                       # the 17-service dev stack
cd difference_service && PYTHONPATH=src:../python_interface python3 -m difference_service.app &
ngrok start --all --config=scripts/ngrok/ngrok.yml        # tunnels (optional)
```

The SPA must be running the **`feature/version-difference-viewer`** branch of
`frontend` — the compare UI does not exist on `main`:

```bash
git -C frontend branch --show-current      # expect feature/version-difference-viewer
```

Open **https://filenginetest.ngrok.io** (or `http://localhost:3000`) and sign in.

---

## 1. Upload the samples

The two folders hold the **same filenames** — that is what makes the second upload
a new *version* of the first file rather than a second file. Do them in order.

1. Create a folder in the file browser, e.g. `diff-review`.
2. Upload **everything in `samples/v1/`** into it. → 36 files, each at version 1.
3. Upload **everything in `samples/v2/`** into the **same folder**.
4. **Check:** the file count is still 36 — not 72. If it doubled, the second
   upload created new files instead of versions; stop and investigate, because
   nothing below will be meaningful.
5. Open any file's details → **Versions**. It should list **two** versions.

---

## 2. The comparison UI

Do this once on `pdf-inserted_object.pdf` to learn the controls.

1. Open the file's details drawer → **Versions** tab.
2. **Check:** ticking versions is limited to **two**; a third tick is refused.
   With exactly two versions on the file, both are already ticked and **Compare
   selected** is ready — there is no decision to make, so you are not asked to
   make one.
3. Click **Compare selected**.
4. **Check:** the ordinary **preview window** opens — the same one that shows the
   document and its comments — now showing the comparison, with the discussion
   rail beside it. There is **no separate comparison window**: if you find
   yourself somewhere that has a comparison but no comments, that is the bug this
   step is looking for.
5. **Check:** while it computes you see *"Preparing the comparison…"*. First run
   on a file takes a few seconds; it should then render on its own without a
   refresh. After ~3 polls it also says a large document can take a while.
6. **Check:** three view buttons — **Before**, **After**, **Difference** — with
   *Difference* selected, and a legend showing red/green/orange.
7. Click between the three views. **Check:** switching is **instant**. It is layer
   visibility inside one already-loaded document, so any network delay here means
   something is wrong.
8. **Check:** the header shows `Before → After` menus and a **Compare** button.
   Pick a different pair. **Check:** the view updates, and the *Before* menu never
   offers a version newer than the one chosen as *After*.
9. Click **← Back to document**. **Check:** you are on the live document, in the
   same window, with the discussion rail untouched.
10. **Check:** with the document showing, **🔀 Compare versions** is offered, and
    it returns you to the comparison controls without going back to the drawer.

### 2.1 Zoom and pan (do this on a drawing, §7.1)

The reason this exists: at fit-width a large sheet's dimension text is a few
pixels tall, so a changed callout is *visible* but not *readable*.

1. Scroll the wheel over the page. **Check:** it zooms, and the point under the
   cursor stays put — it should feel like moving a magnifier over the sheet, not
   like scrolling a picture.
2. **Check:** the percentage in the toolbar tracks the zoom. 100% means *fits the
   window*, not "actual size" — there is no meaningful actual size for an SVG.
3. Zoom in, then drag the page. **Check:** the drawing moves exactly with the
   pointer, and the cursor shows a grab hand. Drag hard toward one corner:
   **Check:** the page cannot be pushed off-screen entirely.
4. **Check:** zoomed in on vector content, lines and text stay **crisp** — the
   comparison is re-rendered at the new scale, not magnified. If it goes blocky
   on a `vector` page, the tier badge is lying or the transform is wrong.
   (A `scanned` page *will* go blocky; that one is genuinely pixels.)
5. Try to zoom out below 100%. **Check:** it stops at the whole page.
6. Step to the next page. **Check:** the zoom is **kept** — checking the same
   detail across sheets should not mean zooming back in each time.
7. Double-click, or click **Fit**. **Check:** back to the whole page.
8. Zoom in and look at the **top-left of the page area**. **Check:** a small
   whole-page mini-map appears, with a box marking the part you are looking at —
   the same navigator the image preview uses.
9. Click somewhere else on the mini-map. **Check:** the view jumps there, centred.
   Drag across the map. **Check:** the page follows continuously, and the box
   cannot be dragged off the edge of the map.
10. Switch between **Before / After / Difference**. **Check:** the mini-map shows
    the same view as the page — you should always be navigating what you are
    looking at.
11. Click **Fit**. **Check:** the mini-map disappears. With the whole page on
    screen there is nothing to navigate to.

### 2.2 Commenting on a comparison

1. With a comparison on screen, zoom into a specific change and click
   **💬 Comment on this comparison**.
2. **Check:** a chip appears above the composer reading *"Comparison attached"*
   with the view and page — not the 3D wording, which would be a plain lie here.
3. Post the comment.
4. Navigate away (Back to document, or close and reopen the file).
5. Find the thread and click **🔀 View comparison**.
6. **Check:** you land on **the same pair, the same page, the same view, and the
   same zoom and pan** — pointing at the detail, not at the sheet. This is the
   whole point: on a B1 drawing, "page 3, difference" is not a location.
7. **Check:** the thread offers **no** 🎯 View or ⬇ BCF buttons. Those need a 3D
   viewpoint; a BCF export of a PDF comparison would produce a file nothing can
   open.
8. Upload a further version of the file. Reopen the comment and click **🔀 View
   comparison** again. **Check:** it still restores the *original* pair. A pair of
   versions is immutable, so its comparison is valid forever — a new upload must
   not disturb it, and must not have deleted it either (that regression is
   covered in §8).

---

## 3. 2D — does it find the right things?

For each: open → **compare** on the newest version → look at **Difference**.

| # | File | Expected |
|---|------|----------|
| 3.1 | `pdf-unchanged.pdf` | **Nothing highlighted.** The control case — any colour here is a false positive. |
| 3.1a | `ifc-unchanged.ifc`, `gltf-unchanged.glb` | The same control for 3D — run these first so a later "nothing changed" result is trustworthy. |
| 3.2 | `pdf-added_object.pdf` | Exactly **one green** rectangle. Nothing else marked. |
| 3.3 | `pdf-deleted_object.pdf` | Exactly **one red** shape. Nothing else marked. |
| 3.4 | `pdf-moved_object.pdf` | **One orange** rectangle (moved a little — still the same object). |
| 3.5 | `pdf-relocated_object.pdf` | **One red + one green** — moved so far it is no longer "the same thing moved". |
| 3.6 | `pdf-restyled_object.pdf` | **One orange** — same geometry, thicker line. |
| 3.7 | `pdf-edited_text.pdf` | The changed line marked; the other text quiet. |
| 3.8 | `pdf-inserted_page.pdf` | 3 pages; the **inserted page entirely green**, its neighbours quiet. |
| 3.9 | `pdf-deleted_page.pdf` | The removed page shown **entirely red**. |

Use the **‹ ›** page arrows on multi-page results; the page counter reads
`Page n / N`.

---

## 4. 2D — the cases that catch a bad implementation

These are the ones worth slowing down on. Each is designed so a plausible-looking
but wrong implementation fails visibly.

### 4.1 `pdf-shifted_page.pdf` — a whole-page shift

Every object moved down by the same amount; nothing was actually edited.

- **Pass:** **nothing** is highlighted.
- **Fail:** the whole page lights up. Technically true and completely useless —
  this is the single most important negative case.

### 4.2 `pdf-inserted_object.pdf` — a mid-page insertion

One object inserted in the middle, everything after it untouched.

- **Pass:** exactly **one green** addition. The heading, the rectangle, the rule
  and the "Notes" text all stay quiet.
- **Fail:** everything after the insertion reads as modified (a cascade).

### 4.3 `pdf-reordered_page.pdf` — pages swapped

- **Pass:** no page reported added or deleted; content matched across the move.

### 4.4 `pdf-scanned.pdf` — an image-only page

- **Pass:** the page renders, the mode badge reads **`scanned`**, and the changed
  region is highlighted rather than individual objects. Hover the badge for why.
- Note: there is no object identity in a scan, so region-level is the honest
  answer here, not a limitation to report.

### 4.5 `pdf-mixed_tier.pdf` — two tiers in one document

- **Pass:** page 1 badge reads **`vector`**, page 2 reads **`scanned`**. Step
  between them with the arrows and watch the badge change.
- This is why the result carries a per-page map instead of one document-wide mode.

---

## 5. 3D — IFC

Open → **compare** → the overlay says the 3D comparison is ready → **Open in the
3D viewer**.

In the viewer, the objects panel has three top-level groups: **old**, **new**,
**difference**. Show/hide/x-ray them with the existing controls — no new viewer
code was added, which is itself part of what you are checking.

| # | File | Expected |
|---|------|----------|
| 5.1 | `ifc-added_element.ifc` | One wall present only in **new** / **difference**. |
| 5.2 | `ifc-deleted_element.ifc` | One wall present only in **old** / **difference**. |
| 5.3 | `ifc-moved_element.ifc` | The wall appears in both, at **different positions**, and in **difference** — it kept its identity, so it is a modification, not a delete plus an add. |
| 5.4 | `ifc-resized_element.ifc` | Same, by size rather than position. |
| 5.4a | `ifc-combined.ifc` | Three at once: one added, one deleted, one moved. |

### 5.5 `ifc-property_only.ifc` — **the one that looks like a bug and is not**

A wall's fire rating changed. Its geometry did not.

- **Pass:** the **difference group is empty** and the model looks unchanged.
  Selecting the wall shows the changed property.
- **This is correct.** Colouring geometry that is pixel-identical trains a
  reviewer to distrust the colour, so a property-only change is recorded and
  surfaced on selection — never painted.
- **Fail:** the wall is orange.

### 5.6 `ifc-renamed_element.ifc` / `ifc-reordered_entities.ifc`

- **Pass:** **nothing** changed in either. A rename is not a change to the model,
  and file order is not identity.

---

### 5.7 One model, one conversation

A comparison of two versions is not a separate model — it is the same model shown
differently, under the same file, sharing that file's comments.

1. Open an IFC in the 3D viewer. **Check:** the title bar offers
   **🔀 Compare versions**, with a `Before → After` picker.
2. Compare two versions. **Check:** the comparison loads **in the same window**,
   the chip reads *🔀 Comparison*, and the comment panel beside it still shows
   the model's existing comments — not an empty list.
3. Frame something changed, select an object, x-ray a few others, then comment.
4. Click **← Back to the model**. **Check:** the plain model returns, and your new
   comment is **still in the list** alongside every other comment on this file.
5. Click **🎯 View** on that comment. **Check:** the viewer **loads the comparison
   again** and restores the full view — camera, selection and x-ray as you left
   them. It should not try to apply the view to the plain model.
6. Click **🎯 View** on a comment made on the *plain* model. **Check:** the viewer
   switches back to the plain model and restores that view.
7. **Check:** older comments made before comparisons existed still restore against
   the plain model — no recorded source means the model itself.

---

## 6. 3D — glTF and CAD (no stable identity)

| # | File | Expected |
|---|------|----------|
| 6.1 | `gltf-added_mesh.glb` | One mesh added. |
| 6.2 | `gltf-moved_mesh.glb` | **Removed volume + added volume** — *not* a "moved" object. |
| 6.3 | `gltf-renamed_node.glb` | **Nothing changed.** glTF node names are not identity; exporters rewrite them freely. |
| 6.4 | `gltf-reordered_nodes.glb` | **Nothing changed.** |
| 6.5 | `cad-moved_solid.step` | Removed + added volume, as 6.2. |
| 6.5a | `cad-resized_solid.step` | Removed + added volume (a resize). |
| 6.6 | `cad-unchanged.step` | **Nothing changed** — proves the CAD tessellation is deterministic. If this one lights up, every CAD comparison is untrustworthy. |
| 6.7 | `gltf-scaled_mesh.glb` | Removed + added volume (a resize, same reasoning as 6.2). |
| 6.8 | `gltf-deleted_mesh.glb` | One mesh removed; the other quiet. |

**Why 6.2 and 6.5 are not bugs:** only IFC carries a durable element id
(GlobalId). glTF and STEP do not, so there is nothing tying a mesh in one version
to a mesh in the other — reporting "the same thing moved" would be a claim the
data cannot support. IFC is the format where a move stays a move (5.3).

---

## 7. Realistic multi-change documents

The cases above isolate one change each. These are closer to a real review: the
interesting question is whether the *unchanged* parts stay quiet.

### 7.1 `revision-drawing.pdf` — a 3-page drawing set

- Page 1: **Rev A → Rev B**, and *Issued for tender* → *Issued for construction*.
  The border and title text around them stay quiet.
- Page 2: a **plant room added** to the floor plan; the existing rooms untouched.
- Page 3: the notes page **shifted down** — content identical. **Pass: nothing
  highlighted.**

That third page is the whole point: one document containing both real edits and a
pure shift, where only the real edits should show.

### 7.2 `mixed-report.pdf`

- Page 1 (`vector`): *Draft → Final*, plus a rule added.
- Page 2 (`scanned`): the image changed → region highlight.

### 7.3 `building-model.ifc`

Three changes at once:

- an internal partition **deleted** (red),
- a party wall **moved** (orange),
- an external wall's **fire rating changed** — **must not be coloured**.

**Pass:** two elements visibly changed, and the third is findable by selection
without being painted.

---

## 8. Edge cases

| # | Do this | Expected |
|---|---------|----------|
| 8.1 | Upload a **single** file (no second version) and open Versions | **No compare action** at all. |
| 8.2 | Upload an image (`.png`) twice to make two versions, then compare | A plain message that automatic comparison isn't available for this type, offering the two versions to download. **Not an error** — images are meant to be flipped between. |
| 8.3 | Compare a file, close the overlay, compare the **same** file again | The second open is **fast** — the result is stored and reused. |
| 8.4 | Compare **while the service is stopped** (`kill` the :8100 process) | A clear error with a **Try again** button — not a hang and not a blank overlay. Restart it and retry. |

---

## 9. Cross-checking against the API

If a UI result looks wrong, this tells you whether the problem is the service or
the front end:

```bash
TOKEN=$(curl -s -X POST localhost:8100/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"username":"testuser@rationalboxes.com","password":"<password>"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s "localhost:8100/files/<FILE_UID>/diff" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Read the response by status:

| Status | HTTP | Meaning |
|--------|------|---------|
| `ready` | 200 | computed; `manifest.units` gives the per-page tiers |
| `pending` | 202 | still computing — poll again |
| `failed` | 422 | attempted and failed; `failure` says where |
| `unsupported` | 200 | no differ for this type (8.2) |
| `none` | 200 | first version, nothing to compare (8.1) |

If the API says `ready` but the UI does not render it, the fault is the front end.
If the API disagrees with a table above, the fault is the service.

---

## 10. Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| No **compare** action anywhere | The SPA is on `main`. Switch `frontend` to `feature/version-difference-viewer` and restart the dev server. |
| Compare opens, then errors immediately | `difference_service` is not running on :8100, or `/diff` is not proxied. Check `curl localhost:8100/healthz` and the Vite proxy. |
| Stuck on "Comparing versions…" | Check the service log; the core (:50051) or LDAP (:1389) may be down — `curl localhost:8100/readyz` shows which. |
| Uploading `v2/` created 36 *new* files | The uploads did not land in the same folder, or names were altered in transit. Delete and redo step 1. |
| Every object on every page is highlighted | A real bug — this is exactly what §4.1 exists to catch. Capture the file and the API response. |
| A scanned page shows no highlight | Expected if the images are identical; check the two versions really do differ. |

---

## What "done" looks like

- §3 all match the expected column.
- §4 all pass — especially 4.1 and 4.2.
- §5.5 shows **no colour** (and that is a pass, not a failure).
- §6.3 and 6.6 show nothing changed.
- §7 shows real edits highlighted and untouched content quiet.
- §8 gives a defined, non-broken outcome in every case.

Anything that disagrees: note the file, what you saw, and the §9 API response —
that triple is enough to locate the fault without reproducing it.
