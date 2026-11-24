-- Each person's signature image (PNG or JPEG, checked by its bytes), printed above "Authorized Signature" on the
-- receipts they record. Kept apart from users so the user list never loads image data. Not synced between
-- offices: a signature belongs to the computer where that person signs in.
CREATE TABLE signatures (
    user_id TEXT PRIMARY KEY REFERENCES users (id),
    content_type TEXT NOT NULL CHECK (content_type IN ('image/png', 'image/jpeg')),
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
