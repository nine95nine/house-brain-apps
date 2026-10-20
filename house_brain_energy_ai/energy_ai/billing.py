"""Read retained statement evidence; never create a competing running bank.

Monthly statements cannot train an hourly/device forecast. A signed bill credit
is kept separate from the ledger's positive settled amount and billed kWh.
"""
from datetime import date
from decimal import Decimal
from .contracts import number

GROUP_LINES = frozenset({'Energy', 'Basic Service and Other Charges', 'Adjustors',
                         'Taxes and Fees', 'Monthly Credits and Other Charges', 'Total Energy Cost'})


def statement_summary(packet):
    if not isinstance(packet, dict) or packet.get('schema') != 'house_brain_aps_statement_summary.v1':
        raise ValueError('STATEMENT_SCHEMA')
    statements = packet.get('statements')
    if not isinstance(statements, list) or not 1 <= len(statements) <= 120:
        raise ValueError('STATEMENT_LIMIT')
    rows = []
    months = set()
    for record in statements:
        label = record['statement_month']
        start, end = date.fromisoformat(record['period_start']), date.fromisoformat(record['period_end'])
        if not start < end or label != end.strftime('%Y-%m') or label in months:
            raise ValueError('STATEMENT_PERIOD')
        months.add(label)
        charges = record['charges']
        if not isinstance(charges, list) or not 1 <= len(charges) <= 128:
            raise ValueError('STATEMENT_CHARGE_LIMIT')
        # The retained extract includes subtotals and itemized lines. Summing
        # both would double-count energy, adjustors, taxes and the grand total.
        detail = sum((Decimal(str(number(c['amount'], -1e6, 1e6))) for c in charges
                      if c['line'] not in GROUP_LINES), Decimal(0))
        total = Decimal(str(number(record['costs']['total_energy_cost'], -1e6, 1e6)))
        rows.append({'statement_month': label, 'period_start': start.isoformat(),
                     'period_end': end.isoformat(), 'days': (end-start).days,
                     'total_energy_cost_usd': float(total), 'itemized_total_usd': float(detail),
                     'itemized_matches': abs(total-detail) <= Decimal('0.02'),
                     'aps_provided_kwh': record['usage_kwh'].get('aps_provided'),
                     'solar_production_kwh': record['usage_kwh'].get('solar_production')})
    rows.sort(key=lambda r: r['statement_month'])
    gaps = sum(a['period_end'] != b['period_start'] for a,b in zip(rows,rows[1:]))
    return {'status': 'RECONCILED' if all(r['itemized_matches'] for r in rows) and not gaps else 'REVIEW',
            'statements': len(rows), 'itemized_discrepancies': sum(not r['itemized_matches'] for r in rows),
            'service_gaps': gaps, 'period_start': rows[0]['period_start'], 'period_end': rows[-1]['period_end'],
            'rows': rows, 'basis': 'retained historical utility statements',
            'hourly_training_history': False, 'current_bank': False, 'savings_claim': False,
            'full_current_bill_forecast': False, 'physical_actions': False}
