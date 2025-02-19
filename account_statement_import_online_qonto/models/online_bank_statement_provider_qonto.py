# Copyright 2020 Florent de Labarre
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json
from datetime import datetime

import pytz
import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.base.models.res_bank import sanitize_account_number

QONTO_ENDPOINT = "https://thirdparty.qonto.com/v2"


class OnlineBankStatementProviderQonto(models.Model):
    _inherit = "online.bank.statement.provider"

    @api.model
    def _get_available_services(self):
        return super()._get_available_services() + [
            ("qonto", "Qonto"),
        ]

    def _obtain_statement_data(self, date_since, date_until):
        self.ensure_one()
        if self.service != "qonto":
            return super()._obtain_statement_data(
                date_since,
                date_until,
            )
        return self._qonto_obtain_statement_data(date_since, date_until)

    def _get_statement_date(self, date_since, date_until):
        self.ensure_one()
        if self.service != "qonto":
            return super()._get_statement_date(
                date_since,
                date_until,
            )
        return date_since.astimezone(pytz.timezone("Europe/Paris")).date()

    #########
    # qonto #
    #########

    def _qonto_header(self):
        self.ensure_one()
        if self.username and self.password:
            return {"Authorization": "{}:{}".format(self.username, self.password)}
        raise UserError(_("Please fill login and key"))

    def _qonto_get_slug(self):
        self.ensure_one()
        url = QONTO_ENDPOINT + "/organizations/%7Bid%7D"
        response = requests.get(
            url, verify=False, headers=self._qonto_header(), timeout=10
        )
        if response.status_code == 200:
            data = json.loads(response.text)
            res = {}
            for account in data.get("organization", {}).get("bank_accounts", []):
                iban = sanitize_account_number(account.get("iban", ""))
                res[iban] = account.get("slug")
            return res
        raise UserError(
            _("%(status_code)s \n\n %(response_text)s")
            % {"status_code": response.status_code, "response_text": response.text}
        )

    def _qonto_obtain_transactions(self, slug, date_since, date_until):
        self.ensure_one()
        url = QONTO_ENDPOINT + "/transactions"
        params = {"slug": slug, "iban": self.account_number}
        # settled_at_to param isn't well formatted (ISO 8601) or year is out of range".
        # We set the last day of the year in such case.
        if date_since and date_until and date_since.year != date_until.year:
            date_until = fields.Datetime.from_string(
                "%s-12-31 23:59:59" % date_since.year
            )
        if date_since:
            params["settled_at_from"] = (
                date_since.replace(microsecond=0).isoformat() + "Z"
            )
        if date_until:
            params["settled_at_to"] = (
                date_until.replace(microsecond=0).isoformat() + "Z"
            )
        transactions = []
        current_page = 1
        total_pages = 1
        while current_page <= total_pages:
            params["current_page"] = current_page
            data = self._qonto_get_transactions(url, params)
            transactions.extend(data.get("transactions", []))
            total_pages = data["meta"]["total_pages"]
            current_page += 1
        return transactions

    def _qonto_get_transactions(self, url, params):
        response = requests.get(
            url,
            verify=False,
            params=params,
            headers=self._qonto_header(),
            timeout=10,
        )
        if response.status_code == 200:
            return json.loads(response.text)
        raise UserError(
            _("%(status_code)s \n\n %(response_text)s")
            % {"status_code": response.status_code, "response_text": response.text}
        )

    def _qonto_prepare_statement_line(
        self, transaction, sequence, journal_currency, currencies_code2id
    ):
        date = datetime.strptime(transaction["settled_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
        side = 1 if transaction["side"] == "credit" else -1
        payment_ref_list = [
            transaction["label"],
            transaction["reference"],
        ]
        vals_line = {
            "sequence": sequence,
            "date": date,
            "name": " - ".join([x for x in payment_ref_list if x]) or "/",
            "ref": transaction["reference"],
            "unique_import_id": transaction["transaction_id"],
            "amount": transaction["amount"] * side,
        }
        if not transaction["local_currency"]:
            raise UserError(
                _(
                    "Transaction ID %(transaction_id)s has no local currency. "
                    "This should never happen."
                )
                % {"transaction_id": transaction["transaction_id"]}
            )
        if transaction["local_currency"] not in currencies_code2id:
            raise UserError(
                _(
                    "Currency %(currency)s used in transaction ID "
                    "%(transaction_id)s doesn't exist in Odoo."
                )
                % {
                    "currency": transaction["local_currency"],
                    "transaction_id": transaction["transaction_id"],
                }
            )
        line_currency_id = currencies_code2id[transaction["local_currency"]]
        if journal_currency.id != line_currency_id:
            vals_line.update(
                {
                    "currency_id": line_currency_id,
                    "amount_currency": transaction["local_amount"] * side,
                }
            )
        return vals_line

    def _qonto_obtain_statement_data(self, date_since, date_until):
        self.ensure_one()
        journal = self.journal_id
        slugs = self._qonto_get_slug()
        slug = slugs.get(self.account_number)
        if not slug:
            raise UserError(
                _("Qonto : wrong configuration, unknow account %s")
                % journal.bank_account_id.acc_number
            )
        transactions = self._qonto_obtain_transactions(slug, date_since, date_until)
        journal_currency = journal.currency_id or journal.company_id.currency_id
        all_currencies = self.env["res.currency"].search_read([], ["name"])
        currencies_code2id = {x["name"]: x["id"] for x in all_currencies}
        new_transactions = []
        sequence = 0
        for transaction in transactions:
            sequence += 1
            vals_line = self._qonto_prepare_statement_line(
                transaction, sequence, journal_currency, currencies_code2id
            )
            new_transactions.append(vals_line)
        if new_transactions:
            return new_transactions, {}
        return
