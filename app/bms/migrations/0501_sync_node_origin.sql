-- A registration's token is issued before the office computer is known; its own node id (the
-- `origin` stamped on its changes) is bound on its first push and may never change afterwards.
ALTER TABLE sync_nodes ADD COLUMN origin_id TEXT;
CREATE UNIQUE INDEX sync_nodes_origin ON sync_nodes (origin_id);
