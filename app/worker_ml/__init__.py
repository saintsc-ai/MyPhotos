"""ML worker package — image classification stages running as a separate
process from the indexing worker.

Inputs are 1024px thumbnails written by the indexing pipeline, so this
worker doesn't need read access to the photo root and never touches the
originals. Outputs land in:

- `photo_tags` (via `tags(source='auto-yolo'|'auto-clip')`) for tag-style filtering
- `photo_embeddings` for similarity search / arbitrary CLIP categories
- `photo_faces` + `face_clusters` for the face cluster UI

Round 1 ships YOLO (object detection); CLIP and faces are scaffolded but
land in subsequent rounds.
"""
