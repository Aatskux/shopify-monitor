import unittest
from product_hunter import store_evidence as ev, market


def response(rows, search=True, marker='VALIDOINTI:'):
    body = 'US: testilähde [1]\n' + marker + '\n'
    annotations = [{'type': 'url_citation', 'start_index': 14, 'end_index': 17,
                    'url': 'https://reference.com/item'}]
    for row, link in rows:
        start = len(body)
        body += row + '\n'
        if link:
            annotations.append({'type': 'url_citation', 'start_index': start,
                                'end_index': len(body)-1, 'url': link})
    output = [{'type': 'web_search_call', 'status': 'completed', 'action': {'type': 'search'}}] if search else []
    output.append({'type': 'message', 'content': [{'type': 'output_text', 'text': body, 'annotations': annotations}]})
    return {'status': 'completed', 'output': output}


class StoreEvidenceTests(unittest.TestCase):
    def test_dedup_original_marketplace_and_citation_gate(self):
        row = 'KAUPPA | ID | - | BAS3B, beige 31 cm'
        other = row.replace('ID', 'KUVA')
        dated = row.replace('| - |', '| 2024-06-04 |')
        data = response([
            (row, 'https://uk.source.co.uk/products/bunny'),
            (row, 'https://seller.co.uk/products/bunny'),
            (dated, 'https://www.seller.co.uk/products/bunny?variant=12'),
            (other, 'https://second-shop.myshopify.com/products/bunny'),
            (row, 'https://www.ebay.co.uk/itm/abc'),
            (row, None),
            ('KAUPPA | SAMANKALTAINEN | - | eri koko', 'https://thirdshop.com/bunny'),
        ])
        result, hits, checked = ev.split(data, 'https://www.source.co.uk/products/bunny', has_photo=True)
        self.assertTrue(checked)
        self.assertEqual(list(hits), ['seller.co.uk', 'second-shop.myshopify.com'])
        self.assertEqual(hits['seller.co.uk'][1], '2024-06-04')
        rendered = ev.section(hits, checked)
        self.assertIn('2 muulla', rendered)
        self.assertIn('?variant=12', rendered)
        self.assertIn('julkaisupäivä 2024-06-04', rendered)
        self.assertNotIn('seller.co.uk', market.render(result))

    def test_unsearched_distinct_from_no_evidence(self):
        args = ([],)
        _, hits, checked = ev.split(response(*args, search=False), 'https://source.com/p')
        self.assertIn('ei voitu varmistaa', ev.section(hits, checked))
        _, hits, checked = ev.split(response(*args, marker='VALIDOINTI: EI_OSUMIA'), 'https://source.com/p')
        self.assertIn('ei löytynyt riittävää', ev.section(hits, checked))

    def test_impossible_date_does_not_turn_listing_into_launch(self):
        _, hits, checked = ev.split(response([
            ('KAUPPA | ID | 2024-02-30 | Sama mallinumero', 'https://seller.com/p/bunny')
        ]), 'https://source.com/p')
        self.assertIn('Julkaisupäiviä ei varmistettu', ev.section(hits, checked))

    def test_markdown_citation_spans_keep_next_row_offsets(self):
        url = 'https://seller.com/products/bunny'
        line = 'Seller | ID | - | BAS3B 31 cm'
        citation = f' ([seller.com]({url}))'
        data = response([(line + citation, url), (line, 'https://other.com/bunny')])
        part = data['output'][-1]['content'][0]
        a = part['annotations'][1]
        a['start_index'] = a['end_index'] - len(citation)
        _, hits, _ = ev.split(data, 'https://source.com/p')
        self.assertEqual(list(hits), ['seller.com', 'other.com'])

    def test_literal_model_must_match_the_original(self):
        _, hits, _ = ev.split(response([
            ('Seller | BAS3BN | - | beige bunny', 'https://mismatch.com/p'),
            ('Seller | BAS3B | - | same model', 'https://match.com/p'),
            ('Seller | KUVA | - | photograph', 'https://unseen.com/p'),
        ]), 'https://source.com/p', 'Bunny BAS3B')
        self.assertEqual(list(hits), ['match.com'])

    def test_citation_on_another_line_is_not_evidence(self):
        _, hits, _ = ev.split(response([
            ('KAUPPA | ID | - | BAS3B', None),
            ('Eri rivi', 'https://seller.com/p/bunny'),
        ]), 'https://source.com/p')
        self.assertFalse(hits)
