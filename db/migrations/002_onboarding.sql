-- Running Khata for a real business: people have emails and can be deactivated;
-- vendors can be deactivated and record who verified their bank details.
SET search_path = khata, public;

ALTER TABLE people ADD COLUMN email text;
ALTER TABLE people ADD COLUMN active boolean NOT NULL DEFAULT true;

ALTER TABLE vendors ADD COLUMN active boolean NOT NULL DEFAULT true;
ALTER TABLE vendors ADD COLUMN bank_verified_by text;
ALTER TABLE vendors ADD COLUMN bank_verified_at timestamptz;
ALTER TABLE vendors ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
