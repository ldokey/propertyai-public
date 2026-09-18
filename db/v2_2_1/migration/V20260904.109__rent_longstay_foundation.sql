-- G1 REVIEW ARTIFACT ONLY. Proposed 109, after immutable 101--108; no application implementation.
SET ROLE propertyai_owner;
ALTER TABLE propertyai.property ADD CONSTRAINT uq_rent_property_org UNIQUE(organization_id,property_id);
CREATE TABLE propertyai.finance_ledger_scope (
 organization_id uuid PRIMARY KEY REFERENCES propertyai.organization(organization_id),
 revision bigint NOT NULL DEFAULT 0 CHECK(revision>=0)
);
CREATE TABLE propertyai.rent_runtime_binding (
 login_name name PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 actor_party_id uuid REFERENCES propertyai.party(party_id),
 principal_type text NOT NULL CHECK(principal_type IN ('PARTY','SYSTEM')),
 capability text NOT NULL CHECK(capability IN ('WRITE','READ','SCHEDULER')),
 data_environment text NOT NULL CHECK(data_environment='TEST'),
 enabled boolean NOT NULL DEFAULT false,
 CHECK((principal_type='PARTY' AND actor_party_id IS NOT NULL AND capability IN ('READ','WRITE'))
    OR (principal_type='SYSTEM' AND actor_party_id IS NULL AND capability='SCHEDULER'))
);
-- This TEST-only adapter binding is provisioned outside migrations by a separately approved fixture.
-- It is NOT a Production finance authority, and cannot be written by runtime roles.
CREATE TABLE propertyai.finance_command (
 command_id uuid PRIMARY KEY REFERENCES propertyai.command_receipt(command_id) DEFERRABLE INITIALLY DEFERRED,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 command_type text NOT NULL CHECK(command_type IN
 ('CREATE_RESIDENT','CREATE_CONTRACT','REVISE_CONTRACT','CREATE_ACCOUNT','REVISE_ACCOUNT',
  'ISSUE_RENT','RECORD_RECEIPT','ALLOCATE','ADJUST_RECEIVABLE','CORRECT_ALLOCATION',
  'CORRECT_MOVEMENT','VOID_RECEIVABLE','RECORD_REFUND','CORRECT_REFUND','CONFIRM_OCCUPANCY_START','CONFIRM_OCCUPANCY_END',
    'CORRECT_OCCUPANCY_DATES','MOVE_OCCUPANCY')),
 idempotency_key uuid NOT NULL,
 request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
 transaction_id xid8 NOT NULL,
 ledger_revision_after bigint NOT NULL CHECK(ledger_revision_after>=0),
 recorded_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,command_id),
 UNIQUE(organization_id,command_type,idempotency_key),
 UNIQUE(transaction_id)
);
CREATE TABLE propertyai.rent_resident (
 resident_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,resident_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 last_command_id uuid NOT NULL,
 FOREIGN KEY(organization_id,last_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 display_name text NOT NULL CHECK(btrim(display_name)<>''),
 linked_party_id uuid REFERENCES propertyai.party(party_id),
 private_contact_ref text,
 review_status text NOT NULL CHECK(review_status IN ('NEEDS_REVIEW','VERIFIED'))
);
CREATE TABLE propertyai.rent_contract (
 contract_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,contract_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 last_command_id uuid NOT NULL,
 FOREIGN KEY(organization_id,last_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 property_id uuid NOT NULL,
 rental_unit_id uuid NOT NULL,
 starts_on date,
 ends_on_exclusive date,
 lifecycle text NOT NULL CHECK(lifecycle IN ('DRAFT','REVIEW_REQUIRED','SIGNED','ACTIVE','RENEWAL_REVIEW','ENDED','TERMINATED','CANCELLED')),
 readiness text NOT NULL CHECK(readiness IN ('NEEDS_REVIEW','READY')),
 current_term_id uuid,
 previous_contract_id uuid,
 change_reason text NOT NULL CHECK(btrim(change_reason)<>''),
 UNIQUE(organization_id,contract_id,property_id,rental_unit_id),
 FOREIGN KEY(organization_id,property_id) REFERENCES propertyai.property(organization_id,property_id),
 FOREIGN KEY(property_id,rental_unit_id) REFERENCES propertyai.rental_unit(property_id,rental_unit_id),
 FOREIGN KEY(organization_id,previous_contract_id) REFERENCES propertyai.rent_contract(organization_id,contract_id),
 CHECK(ends_on_exclusive IS NULL OR starts_on IS NULL OR ends_on_exclusive>starts_on),
 CHECK(previous_contract_id IS DISTINCT FROM contract_id),
 CHECK(readiness<>'READY' OR (starts_on IS NOT NULL AND ends_on_exclusive IS NOT NULL AND current_term_id IS NOT NULL))
);
CREATE TABLE propertyai.rent_contract_history (
 organization_id uuid NOT NULL,
 contract_id uuid NOT NULL,
 version bigint NOT NULL CHECK(version>0),
 command_id uuid NOT NULL,
 snapshot jsonb NOT NULL,
 recorded_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(organization_id,contract_id,version),
 FOREIGN KEY(organization_id,contract_id) REFERENCES propertyai.rent_contract(organization_id,contract_id),
 FOREIGN KEY(organization_id,command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE propertyai.rent_contract_party (
 contract_party_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,contract_party_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 contract_id uuid NOT NULL,
 party_id uuid NOT NULL REFERENCES propertyai.party(party_id),
 party_role text NOT NULL CHECK(party_role IN ('TENANT','SIGNER','GUARANTOR')),
 UNIQUE(organization_id,contract_id,party_id,party_role),
 FOREIGN KEY(organization_id,contract_id) REFERENCES propertyai.rent_contract(organization_id,contract_id)
);
CREATE TABLE propertyai.rent_contract_resident (
 contract_resident_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,contract_resident_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 contract_id uuid NOT NULL,
 resident_id uuid NOT NULL,
 UNIQUE(organization_id,contract_id,resident_id),
 FOREIGN KEY(organization_id,contract_id) REFERENCES propertyai.rent_contract(organization_id,contract_id),
 FOREIGN KEY(organization_id,resident_id) REFERENCES propertyai.rent_resident(organization_id,resident_id)
);
CREATE TABLE propertyai.rent_term_revision (
 term_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,term_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 last_command_id uuid NOT NULL,
 FOREIGN KEY(organization_id,last_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 contract_id uuid NOT NULL,
 revision_no integer NOT NULL CHECK(revision_no>0),
 valid_from date NOT NULL,
 valid_to_exclusive date,
 monthly_rent bigint CHECK(monthly_rent>=0),
 amount_confirmed boolean NOT NULL,
 due_day integer CHECK(due_day BETWEEN 1 AND 31),
 cycle_rule jsonb,
 cycle_confirmed boolean NOT NULL,
 policy_version text NOT NULL CHECK(policy_version='RENT_APPROVED_1G_2G_3G_4G_V1'),
 superseded_by uuid,
 supersession_reason text,
 UNIQUE(organization_id,contract_id,term_id),
 UNIQUE(organization_id,contract_id,revision_no),
 FOREIGN KEY(organization_id,contract_id) REFERENCES propertyai.rent_contract(organization_id,contract_id),
 FOREIGN KEY(organization_id,contract_id,superseded_by) REFERENCES propertyai.rent_term_revision(organization_id,contract_id,term_id) DEFERRABLE INITIALLY DEFERRED,
 CHECK(valid_to_exclusive IS NULL OR valid_to_exclusive>valid_from),
 CHECK(NOT amount_confirmed OR monthly_rent IS NOT NULL),
 CHECK(NOT cycle_confirmed OR (cycle_rule IS NOT NULL AND due_day IS NOT NULL
  AND jsonb_typeof(cycle_rule)='object' AND cycle_rule->>'schema_version'='1'
  AND cycle_rule->>'mode' IN ('CALENDAR_MONTH','EXPLICIT_PERIODS')
  AND cycle_rule ? 'first_due_month' AND cycle_rule ? 'confirmation_ref') IS TRUE),
 CHECK(superseded_by IS NULL OR (superseded_by<>term_id AND supersession_reason IS NOT NULL AND btrim(supersession_reason)<>'')),
 EXCLUDE USING gist (organization_id WITH =, contract_id WITH =,
  daterange(valid_from,valid_to_exclusive,'[)') WITH &&)
 WHERE(superseded_by IS NULL) DEFERRABLE INITIALLY DEFERRED
);
ALTER TABLE propertyai.rent_contract ADD CONSTRAINT fk_rent_current_term
 FOREIGN KEY(organization_id,contract_id,current_term_id)
 REFERENCES propertyai.rent_term_revision(organization_id,contract_id,term_id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE propertyai.rent_billing_period (
 period_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,period_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 contract_id uuid NOT NULL,
 cycle_start date NOT NULL,
 cycle_end_exclusive date NOT NULL,
 due_month date NOT NULL CHECK(extract(day FROM due_month)=1),
 due_on date NOT NULL CHECK(due_on>=due_month AND due_on<(due_month+interval '1 month')::date),
 confirmation_ref text NOT NULL CHECK(btrim(confirmation_ref)<>''),
 CHECK(cycle_end_exclusive>cycle_start),
 UNIQUE(organization_id,contract_id,period_id),
 UNIQUE(organization_id,contract_id,cycle_start,cycle_end_exclusive),
 FOREIGN KEY(organization_id,contract_id) REFERENCES propertyai.rent_contract(organization_id,contract_id),
 EXCLUDE USING gist(organization_id WITH =,contract_id WITH =,
  daterange(cycle_start,cycle_end_exclusive,'[)') WITH &&) DEFERRABLE INITIALLY DEFERRED
);
-- V4 explicit occupancy commands: CONFIRM_OCCUPANCY_START, CONFIRM_OCCUPANCY_END,
-- CORRECT_OCCUPANCY_DATES, MOVE_OCCUPANCY. See contract/Command_Handler_Contract_v4.json.
-- Identity columns remain immutable. MOVE_OCCUPANCY closes old and inserts the linked
-- target-contract/different-unit row in one command/transaction; no intermediate commit.
-- Actual dates/evidence are explicit; optimistic versions and both deferred exclusions
-- remain mandatory. No ACTIVE/ENDED storage enum and no cross-contract sharing exception.
CREATE TABLE propertyai.rent_occupancy (
 occupancy_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,occupancy_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 last_command_id uuid NOT NULL,
 FOREIGN KEY(organization_id,last_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 contract_id uuid NOT NULL,
 resident_id uuid NOT NULL,
 property_id uuid NOT NULL,
 rental_unit_id uuid NOT NULL,
 actual_start date,
 actual_end_exclusive date,
 review_status text NOT NULL CHECK(review_status IN ('NEEDS_REVIEW','VERIFIED')),
 FOREIGN KEY(organization_id,contract_id,property_id,rental_unit_id)
 REFERENCES propertyai.rent_contract(organization_id,contract_id,property_id,rental_unit_id),
 FOREIGN KEY(organization_id,contract_id,resident_id)
 REFERENCES propertyai.rent_contract_resident(organization_id,contract_id,resident_id),
 CHECK(actual_start IS NULL OR actual_end_exclusive IS NULL OR actual_end_exclusive>actual_start),
 CONSTRAINT ck_rent_occupancy_verified_start CHECK(review_status<>'VERIFIED' OR actual_start IS NOT NULL),
 -- Same resident/unit overlap is invalid even within one permitted shared contract.
 EXCLUDE USING gist(organization_id WITH =,resident_id WITH =,rental_unit_id WITH =,
  daterange(actual_start,actual_end_exclusive,'[)') WITH &&)
 WHERE(actual_start IS NOT NULL AND review_status='VERIFIED') DEFERRABLE INITIALLY DEFERRED,
 -- A shared occupancy set is exactly one organization/contract/unit plus explicit resident links.
 -- Different contracts, including previous_contract_id links, do not grant sharing permission.
 CONSTRAINT ex_rent_occupancy_cross_contract EXCLUDE USING gist(
  organization_id WITH =,rental_unit_id WITH =,contract_id WITH <>,
  daterange(actual_start,actual_end_exclusive,'[)') WITH &&)
 WHERE(actual_start IS NOT NULL AND review_status='VERIFIED') DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE propertyai.finance_account (
 account_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,account_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 last_command_id uuid NOT NULL,
 FOREIGN KEY(organization_id,last_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 display_name text NOT NULL CHECK(btrim(display_name)<>''),
 currency text NOT NULL CHECK(currency='KRW'),
 owner_party_id uuid REFERENCES propertyai.party(party_id),
 masked_identifier text NOT NULL,
 protected_identifier_ref text,
 active boolean NOT NULL DEFAULT true
);
