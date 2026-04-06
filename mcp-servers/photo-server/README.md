# Niu Photo Server

MCP server for file and photo management with face recognition.

## Tools

### Document Tools
- `ingest_document` - Document ingestion with conflict handling
- `ingest_documents` - Batch document ingestion

### Photo Tools
- `ingest_photo` - Photo ingestion with face recognition
- `name_person` - Name a detected person
- `merge_persons` - Merge two persons into one

## Photo Processing Features

### Face Recognition
- Uses InsightFace (buffalo_l) for face detection
- Person matching with cosine similarity
- Automatic center embedding updates
- Learning mechanism for threshold adjustment

### EXIF Extraction
- Date/time taken
- GPS coordinates
- Camera information

### L0 Abstract Generation
- Auto-generates photo summary with person names and date
- Example: "张三、李四合影，2026年03月27日"

## Database

Photos, persons, and faces are stored in SQLite at `~/.niu/photos.db`.

### Schema
- `persons` - Person records with center embeddings
- `photos` - Photo records with EXIF and abstract
- `faces` - Face-to-person associations