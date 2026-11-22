"""Research calculation for two already normalized synthetic tables."""

from collections import defaultdict
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import re
import sys
from time import perf_counter


DECLARATION_NUMBER = re.compile(r'[0-9]{8}/[0-9]{6}/[0-9]{7}')


def covers(outer, inner):
    if inner is None:
        return False
    start, end = map(date.fromisoformat, outer)
    first, last = map(date.fromisoformat, inner)
    return start <= first <= last <= end


def repeated_source_rows(rows):
    by_source = defaultdict(list)
    for row in rows:
        by_source[row['row']].append(row)
    return {source: records for source, records in by_source.items() if len(records) > 1}


def reconcile(fts, ledger):
    if not fts['payer'] or fts['payer'] != ledger['payer']:
        return {'stopped': 'payer_mismatch'}

    # Sum every movement from the prepared report before selecting ДТ rows.
    control = {}
    for field in ('incoming', 'outgoing'):
        expected = fts[f'{field}_total_kop']
        actual = sum(row[f'{field}_kop'] for row in fts['rows'])
        control[field] = {'header_kop': expected, 'rows_kop': actual, 'difference_kop': actual - expected}
    if any(value['difference_kop'] for value in control.values()):
        return {'stopped': 'report_totals_mismatch', 'report_control': control}

    repeated = {}
    for source, table in (('fts', fts), ('ledger', ledger)):
        conflicts = repeated_source_rows(table['rows'])
        if conflicts:
            repeated[source] = conflicts
    if repeated:
        return {
            'stopped': 'duplicate_source_row',
            'report_control': control,
            'duplicate_source_rows': repeated,
        }

    movements = defaultdict(list)
    accounts = defaultdict(list)
    exceptions = defaultdict(list)
    out_of_scope = []
    unrecognized = []
    for row in fts['rows']:
        movement_date = row['movement_date']
        if not covers(fts['movement_period'], (movement_date, movement_date)):
            return {'stopped': 'movement_outside_report_period', 'source_row': row['row']}
        kind = row.get('kind')
        if kind == 'ДТ':
            movements[row['document_number']].append(row)
        elif kind in ('КДТ', 'решение о возврате'):
            number = row.get('linked_declaration')
            if not isinstance(number, str) or not DECLARATION_NUMBER.fullmatch(number):
                number = None
            exceptions[number].append(row['row'])
        elif kind == 'залог':
            out_of_scope.append(row)
        else:
            unrecognized.append(row)
    for row in ledger['rows']:
        release_date = row['release_date']
        if not covers(ledger['release_period'], (release_date, release_date)):
            return {'stopped': 'release_outside_ledger_period', 'source_row': row['row']}
        accounts[row['declaration']].append(row)

    coverage_confirmed = ledger.get('coverage_confirmed') is True
    confirmed_declarations = ledger.get('coverage_confirmed_declarations')
    if not isinstance(confirmed_declarations, list):
        confirmed_declarations = []

    groups = {}
    for number in sorted(movements.keys() | accounts.keys()):
        if not DECLARATION_NUMBER.fullmatch(number):
            return {'stopped': 'invalid_declaration_number', 'value': number}
        actual_rows = movements[number]
        ledger_rows = accounts[number]
        actual_kop = sum(row['outgoing_kop'] for row in actual_rows)
        expected_kop = sum(row['initial_kop'] for row in ledger_rows)
        delta_kop = actual_kop - expected_kop
        reasons = []
        if not coverage_confirmed and number not in confirmed_declarations:
            reasons.append('ledger_coverage_unconfirmed')
        for row in ledger_rows:
            interval = row['confirmed_debit_period']
            if not covers(fts['movement_period'], interval):
                reasons.append('debit_period_unconfirmed_or_incomplete')
            elif any(not covers(interval, (item['movement_date'], item['movement_date'])) for item in actual_rows):
                reasons.append('movement_outside_confirmed_debit_period')
            if row['own_account'] is not True or row['domestic_release'] is not True:
                reasons.append('ledger_scope_requires_review')
        if len(ledger_rows) > 1:
            reasons.append('multiple_ledger_rows')
        fingerprints = {
            (row['movement_date'], row['document_date'], row['kbk'], row['incoming_kop'], row['outgoing_kop'])
            for row in actual_rows
        }
        if len(fingerprints) < len(actual_rows):
            reasons.append('possible_duplicate')
        if exceptions.get(number):
            reasons.append('linked_adjustment_or_refund')
        if exceptions.get(None):
            reasons.append('unlinked_adjustment_or_refund')
        if unrecognized:
            reasons.append('unrecognized_movement')

        if reasons:
            status = 'manual_review'
        elif not actual_rows:
            status = 'ledger_only'
        elif not ledger_rows:
            status = 'report_only'
        else:
            status = 'amount_difference' if delta_kop else 'equal'
        groups[number] = {
            'status': status, 'reasons': sorted(set(reasons)),
            'ledger_kop': expected_kop, 'report_kop': actual_kop, 'delta_kop': delta_kop,
            'ledger_rows': [row['row'] for row in ledger_rows],
            'report_rows': [row['row'] for row in actual_rows],
            'linked_exception_rows': exceptions.get(number, []),
        }
    return {
        'report_control': control,
        'unlinked_exception_rows': exceptions.get(None, []),
        'exception_rows_for_other_declarations': {
            number: rows for number, rows in exceptions.items() if number is not None and number not in groups
        },
        'out_of_scope_rows': out_of_scope,
        'unrecognized_movement_rows': unrecognized,
        'groups': groups,
    }


def check(label, actual, expected):
    if actual != expected:
        raise AssertionError(f'{label}: expected {expected!r}, got {actual!r}')


def verified_cases(fts, ledger):
    report = {'equal_totals': reconcile(fts, ledger)}
    groups = report['equal_totals']['groups']
    check('baseline groups', len(groups), 5)
    check('baseline ledger total', sum(row['ledger_kop'] for row in groups.values()), 3230000)
    check('baseline report total', sum(row['report_kop'] for row in groups.values()), 3230000)
    expected = [
        ('equal', 0, ['УЧ-01'], ['ФТС-01']),
        ('amount_difference', -30000, ['УЧ-02'], ['ФТС-02']),
        ('amount_difference', 30000, ['УЧ-03'], ['ФТС-03']),
        ('ledger_only', -230000, ['УЧ-04'], []),
        ('report_only', 230000, [], ['ФТС-04']),
    ]
    for number, outcome in zip(sorted(groups), expected):
        row = groups[number]
        check(number, (row['status'], row['delta_kop'], row['ledger_rows'], row['report_rows']), outcome)

    first = ledger['rows'][0]['declaration']
    unconfirmed_ledger = dict(ledger, coverage_confirmed=False)
    without_confirmation = {key: value for key, value in ledger.items() if key != 'coverage_confirmed'}
    for case, account in [('unconfirmed_ledger_coverage', unconfirmed_ledger),
                          ('missing_ledger_coverage_confirmation', without_confirmation)]:
        report[case] = reconcile(fts, account)
        expected_review = {
            number: dict(row, status='manual_review', reasons=['ledger_coverage_unconfirmed'])
            for number, row in groups.items()
        }
        check(case, report[case]['groups'], expected_review)

    confirmed = [number for number in groups if number != first]
    partial_coverage = dict(unconfirmed_ledger, coverage_confirmed_declarations=confirmed)
    report['partially_confirmed_ledger_coverage'] = reconcile(fts, partial_coverage)
    expected_partial = dict(groups)
    expected_partial[first] = dict(groups[first], status='manual_review', reasons=['ledger_coverage_unconfirmed'])
    check('confirmed declarations retain their statuses',
          report['partially_confirmed_ledger_coverage']['groups'], expected_partial)

    restored = deepcopy(ledger)
    restored['rows'].append(dict(ledger['rows'][0], row='УЧ-05', initial_kop=50000))
    report['restored_ledger_entry'] = reconcile(fts, restored)
    expected_restored = dict(groups)
    expected_restored[first] = dict(groups[first], status='manual_review', reasons=['multiple_ledger_rows'],
                                    ledger_kop=1300000, delta_kop=-50000, ledger_rows=['УЧ-01', 'УЧ-05'])
    check('restored ledger entry exposes the difference', report['restored_ledger_entry']['groups'], expected_restored)

    covered_part = deepcopy(ledger)
    covered_part['rows'][0]['confirmed_debit_period'] = ['2026-09-10', '2026-10-02']
    missing_period = deepcopy(ledger)
    missing_period['rows'][0]['confirmed_debit_period'] = None
    for case, account in [('incomplete_period', covered_part), ('missing_period', missing_period)]:
        report[case] = reconcile(fts, account)
        row = report[case]['groups'][first]
        check(case, (row['status'], row['delta_kop'], row['reasons']),
              ('manual_review', 0, ['debit_period_unconfirmed_or_incomplete']))

    split = deepcopy(fts)
    split['rows'][0]['outgoing_kop'] = 625000
    split['rows'].append(dict(split['rows'][0], row='ФТС-05'))
    report['possible_duplicate'] = reconcile(split, ledger)
    row = report['possible_duplicate']['groups'][first]
    check('duplicate', (row['status'], row['delta_kop'], row['reasons'], row['report_rows']),
          ('manual_review', 0, ['possible_duplicate'], ['ФТС-01', 'ФТС-05']))
    split['rows'][-1]['kbk'] = 'КБК-2'
    report['different_kbk'] = reconcile(split, ledger)
    row = report['different_kbk']['groups'][first]
    check('different KBK', (row['status'], row['report_kop'], row['report_rows']),
          ('equal', 1250000, ['ФТС-01', 'ФТС-05']))

    same_source = deepcopy(split)
    same_source['rows'][-1]['row'] = 'ФТС-01'
    report['duplicate_report_source'] = reconcile(same_source, ledger)
    check('repeated report source with different KBK', report['duplicate_report_source'], {
        'stopped': 'duplicate_source_row', 'report_control': report['equal_totals']['report_control'],
        'duplicate_source_rows': {'fts': {'ФТС-01': [same_source['rows'][0], same_source['rows'][-1]]}},
    })
    repeated_ledger = deepcopy(ledger)
    repeated_ledger['rows'][1]['row'] = 'УЧ-01'
    report['duplicate_ledger_source'] = reconcile(fts, repeated_ledger)
    check('repeated ledger source across declarations', report['duplicate_ledger_source'], {
        'stopped': 'duplicate_source_row', 'report_control': report['equal_totals']['report_control'],
        'duplicate_source_rows': {'ledger': {'УЧ-01': repeated_ledger['rows'][:2]}},
    })

    adjusted = deepcopy(fts)
    adjusted['rows'].append(dict(fts['rows'][0], row='ФТС-05', kind='КДТ',
                                 document_number='УЧЕБНАЯ-КДТ', linked_declaration=first, outgoing_kop=50000))
    adjusted['outgoing_total_kop'] += 50000
    report['linked_adjustment'] = reconcile(adjusted, ledger)
    row = report['linked_adjustment']['groups'][first]
    check('linked adjustment', (row['status'], row['report_kop'], row['reasons'], row['linked_exception_rows']),
          ('manual_review', 1250000, ['linked_adjustment_or_refund'], ['ФТС-05']))
    exception_row = adjusted['rows'][-1]
    del exception_row['linked_declaration']
    for case, reference_fields in [
        ('unlinked_adjustment', {'linked_declaration': None}),
        ('empty_adjustment_link', {'linked_declaration': ''}),
        ('truncated_adjustment_link', {'linked_declaration': first[:-1]}),
        ('missing_adjustment_link', {}),
    ]:
        adjusted['rows'][-1] = dict(exception_row, **reference_fields)
        unlinked = report[case] = reconcile(adjusted, ledger)
        check(f'{case} source', unlinked['unlinked_exception_rows'], ['ФТС-05'])
        check(f'{case} other declarations', unlinked['exception_rows_for_other_declarations'], {})
        check(f'{case} group numbers', unlinked['groups'].keys(), groups.keys())
        for number, row in unlinked['groups'].items():
            check(f'{case} {number}', (row['status'], row['delta_kop'], row['reasons']),
                  ('manual_review', groups[number]['delta_kop'], ['unlinked_adjustment_or_refund']))

    outside = first[:-7] + '0000009'
    adjusted['rows'][-1].update(kind='решение о возврате', linked_declaration=outside)
    other = report['refund_for_another_declaration'] = reconcile(adjusted, ledger)
    check('outside source', other['exception_rows_for_other_declarations'], {outside: ['ФТС-05']})
    check('outside unresolved sources', other['unlinked_exception_rows'], [])
    check('outside groups', other['groups'], groups)

    movement = deepcopy(fts)
    extra = dict(fts['rows'][0], row='CHECK-X', kind='не распознано',
                 document_number='CHECK-X', linked_declaration=None, outgoing_kop=50000)
    movement['outgoing_total_kop'] += extra['outgoing_kop']
    movement['rows'].append(extra)
    unknown = report['unrecognized_movement'] = reconcile(movement, ledger)
    check('unknown movement control', unknown['report_control']['outgoing'],
          {'header_kop': 3280000, 'rows_kop': 3280000, 'difference_kop': 0})
    check('unknown movement source', unknown['unrecognized_movement_rows'], [extra])
    check('unknown movement excluded rows', unknown['out_of_scope_rows'], [])
    check('unknown movement preserves all groups', unknown['groups'].keys(), groups.keys())
    for number, row in unknown['groups'].items():
        check(f'unknown movement {number}', (row['status'], row['delta_kop'], row['reasons']),
              ('manual_review', groups[number]['delta_kop'], ['unrecognized_movement']))

    pledge = dict(extra, row='CHECK-PLEDGE', kind='залог', document_number='CHECK-PLEDGE')
    movement['rows'][-1] = pledge
    excluded = report['known_out_of_scope_movement'] = reconcile(movement, ledger)
    check('out-of-scope movement control', excluded['report_control'], unknown['report_control'])
    check('out-of-scope movement source', excluded['out_of_scope_rows'], [pledge])
    check('out-of-scope movement unknown rows', excluded['unrecognized_movement_rows'], [])
    check('out-of-scope movement preserves statuses', excluded['groups'], groups)

    truncated = deepcopy(fts)
    truncated['rows'].pop()
    report['omitted_movement'] = reconcile(truncated, ledger)
    check('missing movement', report['omitted_movement'], {
        'stopped': 'report_totals_mismatch',
        'report_control': {'incoming': {'header_kop': 0, 'rows_kop': 0, 'difference_kop': 0},
                           'outgoing': {'header_kop': 3230000, 'rows_kop': 3000000, 'difference_kop': -230000}},
    })
    missing_incoming = dict(fts, incoming_total_kop=100000)
    report['omitted_incoming'] = reconcile(missing_incoming, ledger)
    check('incoming total', report['omitted_incoming'], {
        'stopped': 'report_totals_mismatch',
        'report_control': {'incoming': {'header_kop': 100000, 'rows_kop': 0, 'difference_kop': -100000},
                           'outgoing': {'header_kop': 3230000, 'rows_kop': 3230000, 'difference_kop': 0}},
    })
    report['payer_mismatch'] = reconcile(fts, dict(ledger, payer='TRAINING-PAYER-2'))
    check('payer mismatch', report['payer_mismatch'], {'stopped': 'payer_mismatch'})
    return report


def main():
    path = Path(__file__).resolve().parent
    source = json.loads((path / 'input.json').read_text(encoding='utf-8'))
    fts, ledger = source['fts'], source['ledger']
    for table in (fts, ledger):
        table['rows'] = [dict(zip(table['columns'], row, strict=True)) for row in table['rows']]
    started = perf_counter()
    scenarios = verified_cases(fts, ledger)
    elapsed = perf_counter() - started
    result = {'data_kind': source['data_kind'], 'scenarios_checked': len(scenarios), 'scenarios': scenarios}
    output = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    target = path / 'result.json'
    if sys.argv[1:] == ['--verify']:
        check('saved report', output, target.read_text(encoding='utf-8'))
    elif not sys.argv[1:]:
        target.write_text(output, encoding='utf-8')
    else:
        raise SystemExit('Usage: reconcile.py [--verify]')
    print(f'{len(scenarios)} scenarios verified in {elapsed:.6f} s (data preparation and file I/O excluded)')


if __name__ == '__main__':
    main()
