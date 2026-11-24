-- The office's answer to "what does this MRPH type code mean": {code: {category, status}} as JSON,
-- chosen once on the batch page and applied to every homeowner carrying that code.
ALTER TABLE import_batches ADD COLUMN type_mapping TEXT NOT NULL DEFAULT '{}';
