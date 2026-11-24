-- Why a rate version was made (board decision, temporary change...), shown in the rates history.
-- Site visit 2026-11-09: "add comments on why a version changed".
ALTER TABLE rate_plans ADD COLUMN note TEXT NOT NULL DEFAULT '';
