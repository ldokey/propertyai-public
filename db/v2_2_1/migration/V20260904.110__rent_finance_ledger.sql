-- G1 REVIEW ARTIFACT ONLY. Proposed 110. P1 core and bounded P2 corrections/refund storage.
SET ROLE propertyai_owner;
CREATE TABLE propertyai.finance_receivable (
 receivable_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,receivable_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 last_command_id uuid NOT NULL,
 FOREIGN KEY(organization_id,last_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 contract_id uuid NOT NULL,
 property_id uuid NOT NULL,
 period_id uuid NOT NULL,
 origin_kind text NOT NULL CHECK(origin_kind='RENT'),
 currency text NOT NULL CHECK(currency='KRW'),
 original_amount bigint NOT NULL CHECK(original_amount>=0),
 calculation_snapshot jsonb NOT NULL CHECK(jsonb_typeof(calculation_snapshot)='object'),
 calculation_sha256 text NOT NULL CHECK(calculation_sha256 ~ '^[0-9a-f]{64}$'),
 due_on date NOT NULL,
 voided boolean NOT NULL DEFAULT false,
 void_reason text,
 replacement_of uuid,
 UNIQUE(organization_id,receivable_id,period_id),
 UNIQUE(organization_id,replacement_of),
 FOREIGN KEY(organization_id,replacement_of,period_id) REFERENCES propertyai.finance_receivable(organization_id,receivable_id,period_id),
 CHECK(replacement_of IS DISTINCT FROM receivable_id),
 FOREIGN KEY(organization_id,contract_id,period_id) REFERENCES propertyai.rent_billing_period(organization_id,contract_id,period_id),
 FOREIGN KEY(organization_id,property_id) REFERENCES propertyai.property(organization_id,property_id),
 CHECK(NOT voided OR (void_reason IS NOT NULL AND btrim(void_reason)<>''))
);
CREATE TABLE propertyai.finance_receivable_line (
 line_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,line_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 receivable_id uuid NOT NULL,
 term_id uuid NOT NULL,
 line_no integer NOT NULL CHECK(line_no>0),
 service_from date NOT NULL,
 service_to_exclusive date NOT NULL,
 monthly_rent bigint NOT NULL CHECK(monthly_rent>=0),
 month_days integer NOT NULL CHECK(month_days BETWEEN 28 AND 31),
 charge_days integer NOT NULL CHECK(charge_days BETWEEN 1 AND 31),
 original_amount bigint NOT NULL CHECK(original_amount>=0),
 CHECK(service_to_exclusive>service_from AND service_to_exclusive-service_from=charge_days),
 UNIQUE(organization_id,receivable_id,line_no),
 FOREIGN KEY(organization_id,receivable_id) REFERENCES propertyai.finance_receivable(organization_id,receivable_id),
 FOREIGN KEY(organization_id,term_id) REFERENCES propertyai.rent_term_revision(organization_id,term_id)
);
CREATE TABLE propertyai.finance_receivable_adjustment (
 adjustment_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,adjustment_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 receivable_id uuid NOT NULL,
 delta bigint NOT NULL CHECK(delta<>0 AND delta>=-9223372036854775807),
 reason text NOT NULL CHECK(btrim(reason)<>''),
 reverse_of uuid,
 UNIQUE(organization_id,reverse_of),
 UNIQUE(organization_id,adjustment_id,receivable_id),
 FOREIGN KEY(organization_id,receivable_id) REFERENCES propertyai.finance_receivable(organization_id,receivable_id),
 FOREIGN KEY(organization_id,reverse_of,receivable_id) REFERENCES propertyai.finance_receivable_adjustment(organization_id,adjustment_id,receivable_id),
 CHECK(reverse_of IS DISTINCT FROM adjustment_id)
);
CREATE TABLE propertyai.finance_movement (
 movement_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,movement_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 last_command_id uuid NOT NULL,
 FOREIGN KEY(organization_id,last_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 direction text NOT NULL CHECK(direction IN ('IN','OUT')),
 replaces_movement_id uuid,
 UNIQUE(organization_id,replaces_movement_id),
 FOREIGN KEY(organization_id,replaces_movement_id) REFERENCES propertyai.finance_movement(organization_id,movement_id),
 CHECK(replaces_movement_id IS DISTINCT FROM movement_id),
 origin text NOT NULL CHECK(origin='MANUAL'),
 current_revision integer NOT NULL CHECK(current_revision>0)
);
CREATE TABLE propertyai.finance_movement_revision (
 movement_revision_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,movement_revision_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 movement_id uuid NOT NULL,
 revision_no integer NOT NULL CHECK(revision_no>0),
 previous_revision_no integer,
 account_id uuid NOT NULL,
 occurred_on date NOT NULL,
 amount bigint NOT NULL CHECK(amount>0),
 currency text NOT NULL CHECK(currency='KRW'),
 payer_raw text,
 counterparty_party_id uuid REFERENCES propertyai.party(party_id),
 record_status text NOT NULL CHECK(record_status IN ('RECORDED','REVERSED')),
 reason text,
 UNIQUE(organization_id,movement_id,revision_no),
 FOREIGN KEY(organization_id,movement_id) REFERENCES propertyai.finance_movement(organization_id,movement_id),
 FOREIGN KEY(organization_id,account_id) REFERENCES propertyai.finance_account(organization_id,account_id),
 FOREIGN KEY(organization_id,movement_id,previous_revision_no)
  REFERENCES propertyai.finance_movement_revision(organization_id,movement_id,revision_no),
 CHECK((revision_no=1 AND previous_revision_no IS NULL AND record_status='RECORDED')
   OR (revision_no>1 AND previous_revision_no=revision_no-1 AND reason IS NOT NULL AND btrim(reason)<>''))
);
ALTER TABLE propertyai.finance_movement ADD CONSTRAINT fk_rent_current_movement_revision
 FOREIGN KEY(organization_id,movement_id,current_revision)
 REFERENCES propertyai.finance_movement_revision(organization_id,movement_id,revision_no) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE propertyai.finance_funding_source (
 source_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,source_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 last_command_id uuid NOT NULL,
 FOREIGN KEY(organization_id,last_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 origin text NOT NULL CHECK(origin='RECEIPT'),
 movement_id uuid NOT NULL,
 attribution_status text NOT NULL CHECK(attribution_status IN ('UNMATCHED','PROPERTY_CONFIRMED','CONTRACT_CONFIRMED')),
 attributed_property_id uuid,
 attributed_contract_id uuid,
 UNIQUE(organization_id,movement_id),
 FOREIGN KEY(organization_id,movement_id) REFERENCES propertyai.finance_movement(organization_id,movement_id),
 FOREIGN KEY(organization_id,attributed_property_id) REFERENCES propertyai.property(organization_id,property_id),
 FOREIGN KEY(organization_id,attributed_contract_id) REFERENCES propertyai.rent_contract(organization_id,contract_id),
 CHECK((attribution_status='UNMATCHED' AND attributed_property_id IS NULL AND attributed_contract_id IS NULL)
 OR (attribution_status='PROPERTY_CONFIRMED' AND attributed_property_id IS NOT NULL AND attributed_contract_id IS NULL)
 OR (attribution_status='CONTRACT_CONFIRMED' AND attributed_property_id IS NOT NULL AND attributed_contract_id IS NOT NULL))
);
CREATE TABLE propertyai.finance_allocation (
 allocation_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,allocation_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 source_id uuid NOT NULL,
 receivable_id uuid NOT NULL,
 amount bigint NOT NULL CHECK(amount>0),
 record_kind text NOT NULL CHECK(record_kind IN ('APPLY','REVERSE')),
 reverse_of uuid,
 attribution_confirmed boolean NOT NULL CHECK(attribution_confirmed),
 override_attribution_reason text,
 reason text,
 UNIQUE(organization_id,reverse_of),
 UNIQUE(organization_id,allocation_id,source_id,receivable_id,amount),
 FOREIGN KEY(organization_id,source_id) REFERENCES propertyai.finance_funding_source(organization_id,source_id),
 FOREIGN KEY(organization_id,receivable_id) REFERENCES propertyai.finance_receivable(organization_id,receivable_id),
 FOREIGN KEY(organization_id,reverse_of,source_id,receivable_id,amount)
 REFERENCES propertyai.finance_allocation(organization_id,allocation_id,source_id,receivable_id,amount),
 CHECK((record_kind='APPLY' AND reverse_of IS NULL)
 OR(record_kind='REVERSE' AND reverse_of IS NOT NULL AND reverse_of<>allocation_id AND reason IS NOT NULL AND btrim(reason)<>''))
);
CREATE TABLE propertyai.finance_source_return (
 return_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL REFERENCES propertyai.organization(organization_id),
 created_command_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(organization_id,return_id),
 FOREIGN KEY(organization_id,created_command_id) REFERENCES propertyai.finance_command(organization_id,command_id) DEFERRABLE INITIALLY DEFERRED,
 source_id uuid NOT NULL,
 outgoing_movement_id uuid NOT NULL,
 amount bigint NOT NULL CHECK(amount>0),
 record_kind text NOT NULL CHECK(record_kind IN ('APPLY','REVERSE')),
 reverse_of uuid,
 reason text NOT NULL CHECK(btrim(reason)<>''),
 actual_transfer_confirmed boolean NOT NULL CHECK(actual_transfer_confirmed),
 UNIQUE(organization_id,reverse_of),
 UNIQUE(organization_id,return_id,source_id,outgoing_movement_id,amount),
 FOREIGN KEY(organization_id,source_id) REFERENCES propertyai.finance_funding_source(organization_id,source_id),
 FOREIGN KEY(organization_id,outgoing_movement_id) REFERENCES propertyai.finance_movement(organization_id,movement_id),
 FOREIGN KEY(organization_id,reverse_of,source_id,outgoing_movement_id,amount)
 REFERENCES propertyai.finance_source_return(organization_id,return_id,source_id,outgoing_movement_id,amount),
 CHECK((record_kind='APPLY' AND reverse_of IS NULL)
 OR(record_kind='REVERSE' AND reverse_of IS NOT NULL AND reverse_of<>return_id))
);
CREATE INDEX ix_rent_36905504fe22 ON propertyai.rent_contract(organization_id,rental_unit_id,lifecycle);
CREATE INDEX ix_rent_70f995833e48 ON propertyai.rent_contract_party(organization_id,party_id);
CREATE INDEX ix_rent_d3b20e837e7a ON propertyai.rent_contract_resident(organization_id,resident_id);
CREATE INDEX ix_rent_32e2eee8fbdc ON propertyai.rent_occupancy(organization_id,contract_id);
CREATE INDEX ix_rent_28320c7b13e7 ON propertyai.finance_receivable(organization_id,contract_id,due_on);
CREATE INDEX ix_rent_23b7783e5c3f ON propertyai.finance_receivable(organization_id,property_id,due_on);
CREATE INDEX ix_rent_dceca116875b ON propertyai.finance_receivable_line(organization_id,term_id);
CREATE INDEX ix_rent_8146be71e647 ON propertyai.finance_receivable_adjustment(organization_id,receivable_id);
CREATE INDEX ix_rent_f49004a06d5e ON propertyai.finance_movement_revision(organization_id,account_id,occurred_on);
CREATE INDEX ix_rent_c040741c0608 ON propertyai.finance_allocation(organization_id,source_id);
CREATE INDEX ix_rent_402589ca355c ON propertyai.finance_allocation(organization_id,receivable_id);
CREATE INDEX ix_rent_5bb4ec0339e8 ON propertyai.finance_source_return(organization_id,source_id);
CREATE INDEX ix_rent_e15bd48f177c ON propertyai.finance_source_return(organization_id,outgoing_movement_id);

CREATE UNIQUE INDEX uq_rent_active_period ON propertyai.finance_receivable(organization_id,period_id) WHERE NOT voided;
CREATE UNIQUE INDEX uq_rent_root_period ON propertyai.finance_receivable(organization_id,period_id) WHERE replacement_of IS NULL;
