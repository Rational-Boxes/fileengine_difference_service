difference_service — manual verification samples
================================================

HOW TO USE
  1. Upload everything in v1/ into a folder in the web UI.
  2. Upload everything in v2/ into the SAME folder.
     The names match, so each upload becomes version 2 of the existing
     file rather than a new file.
  3. Open a file's details -> Versions -> 'compare' on the newest version.

Colour convention: red = deleted, green = added, orange = modified.

These are the same fixtures the automated tests run against, so what you
see here is what CI asserts.

CASES THAT ARE EASY TO GET WRONG
------------------------------------------------
  pdf/shifted_page
      every object moved by the same amount; a naive tool reports the WHOLE page modified. Expect: nothing changed.

  pdf/inserted_object
      one object inserted mid-page; a naive tool re-flows everything after it. Expect: exactly one addition, nothing else touched.

  pdf/mixed_tier
      one vector page + one scanned page in the same document; each page should be handled on its own terms.

  ifc/property_only
      a property changed, geometry did not. Expect the model to look UNCHANGED — the change is recorded, not painted. Colouring it would be the bug.

  ifc/reordered_entities
      the same elements written in a different order. Expect: nothing changed.

  gltf/renamed_node
      only a node name changed. Expect: nothing changed — glTF names are not identity, and exporters rewrite them freely.

ALL SAMPLES
------------------------------------------------
  file                               expected result
  pdf-unchanged.pdf                  nothing changed; no object may report modified
  pdf-added_object.pdf               1 added at end; rest unchanged
  pdf-deleted_object.pdf             1 deleted from middle; rest unchanged
  pdf-inserted_object.pdf            1 added mid-stream; trailing objects MUST stay unchanged
  pdf-moved_object.pdf               1 modified by small translation
  pdf-relocated_object.pdf           moved beyond threshold => deleted + added
  pdf-restyled_object.pdf            1 modified by style (stroke width)
  pdf-edited_text.pdf                1 text run's string changed in place
  pdf-shifted_page.pdf               whole page translated; semantically unchanged
  pdf-inserted_page.pdf              page inserted; neighbours unchanged
  pdf-deleted_page.pdf               middle page removed
  pdf-reordered_page.pdf             pages swapped; content unchanged
  pdf-mixed_tier.pdf                 vector page + scanned page => mode 'mixed'
  pdf-scanned.pdf                    image-only; raster tier, changed region
  ifc-unchanged.ifc                  nothing changed
  ifc-added_element.ifc              WALL_C added
  ifc-deleted_element.ifc            WALL_C deleted
  ifc-moved_element.ifc              WALL_B modified (geometry: placement)
  ifc-resized_element.ifc            WALL_A modified (geometry: profile)
  ifc-property_only.ifc              WALL_A property changed => NO visual delta
  ifc-renamed_element.ifc            WALL_A name changed => NO visual delta
  ifc-reordered_entities.ifc         entity order differs; nothing changed
  ifc-combined.ifc                   C added, A deleted, B moved
  gltf-unchanged.glb                 nothing changed; empty difference volume
  gltf-added_mesh.glb                one mesh added
  gltf-deleted_mesh.glb              one mesh deleted
  gltf-moved_mesh.glb                translated => deleted volume + added volume
  gltf-scaled_mesh.glb               scaled => partial overlap, shell is the delta
  gltf-renamed_node.glb              name changed only; nothing changed
  gltf-reordered_nodes.glb           node order differs; nothing changed
  cad-unchanged.step                 same solid; tessellation must be deterministic
  cad-resized_solid.step             dimensions changed => volume delta
  cad-moved_solid.step               translated => removed + added volume
  revision-drawing.pdf               3 pages: Rev A->B title block + issue status changed; a plant room added to the plan; the notes page shifted down but is otherwise identical (it must NOT read as rewritten).
  mixed-report.pdf                   2 pages: a vector cover (Draft->Final, a rule added) and a scanned page whose image changed. The document should report mode 'mixed'.
  building-model.ifc                 IFC: internal partition deleted, party wall moved, external wall's fire rating changed. The fire-rating change must NOT colour the wall.

NOTES
------------------------------------------------
  * A first version has no predecessor, so 'compare' is only offered from
    the second version onward.
  * A comparison runs on the server and can take a few seconds; the UI
    shows it computing and then loads the result.
  * gltf/*.glb and cad/*.step have no stable element ids, so a moved or
    resized object is reported as removed + added volume rather than
    'the same thing, changed'. That is the honest answer for those
    formats — only IFC carries durable identity (GlobalId).
