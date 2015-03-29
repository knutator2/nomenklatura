import logging

import dedupe

from nomenklatura.core import db, celery
from nomenklatura.schema import attributes
from nomenklatura.model.dataset import Dataset
from nomenklatura.model.pairing import Pairing
from nomenklatura.query import EntityQuery, execute_query

KEEP_SIZE = 20
log = logging.getLogger(__name__)


def make_fields():
    fields = []
    for attr in attributes:
        if attr.data_type in ['string', 'text']:
            fields.append({
                'field': attr.name,
                'type': 'Set' if attr.many else 'String',
                'has missing': True
            })
        if attr.data_type in ['type']:
            fields.append({
                'field': attr.name,
                'type': 'Exact'
            })
    return fields


def query_pairings(dataset, decided):
    q = db.session.query(Pairing)
    q = q.filter(Pairing.dataset == dataset)
    q = q.filter(Pairing.decided == decided) # noqa
    return q


def make_data(dataset, fields):
    data = {}
    q = {'limit': None}
    for e in EntityQuery(dataset, q):
        ent = {}
        for field in fields:
            name = field.get('field')
            ent[name] = e.get(name)
            if isinstance(ent[name], list):
                ent[name] = tuple([unicode(v) or '' for v in ent[name]])
            else:
                ent[name] = unicode(ent[name]) or ''
        data[e.id] = ent
    return data


def make_pairs(dataset, data):
    pairs = {'match': [], 'distinct': []}
    for pairing in query_pairings(dataset, True):
        if pairing.left_id not in data or pairing.right_id not in data:
            continue
        pair = (data.get(pairing.left_id), data.get(pairing.right_id))
        if pairing.decision is True:
            pairs['match'].append(pair)
        if pairing.decision is False:
            pairs['distinct'].append(pair)
    return pairs


@celery.task
def dedupe_generate_pairings(slug, threshold=15):
    dataset = Dataset.by_slug(slug)
    num = query_pairings(dataset, True).count()

    # do this only on full moon.
    if num < threshold or num % threshold != 0:
        return

    log.info("Dedupe to generate pairings candidates: %s", slug)
    fields = make_fields()
    data = make_data(dataset, fields)
    pairs = make_pairs(dataset, data)

    deduper = dedupe.Dedupe(fields)
    deduper.sample(data)
    deduper.markPairs(pairs)
    deduper.train()

    matches = []
    for match in deduper.match(data):
        scored = sorted(zip(match[0], match[1]),
                        key=lambda (id, s): s, reverse=True)
        scored = list(scored)[:2]
        (e1, s1), (e2, s2) = scored
        score = ((s1 + s2) / 2.0) * 100.0
        matches.append((e1, e2, score))

    matches = sorted(matches, key=lambda (e, a, s): s, reverse=True)
    for (left_id, right_id, score) in matches:
        Pairing.update({'left_id': left_id, 'right_id': right_id},
                       dataset, None, score=score)


@celery.task
def generate_pairings(slug, threshold=30):
    dataset = Dataset.by_slug(slug)

    # training_size = query_pairings(dataset, True).count()
    # if training_size > threshold and training_size % threshold != 0:
    #     dedupe_generate_pairings.delay(slug)

    while query_pairings(dataset, False).count() < KEEP_SIZE:
        generate_best_random_pairing(dataset)


def generate_random_pairing(dataset):
    query = {
        'label': None,
        'sort': 'random',
        'same_as': {'optional': 'forbidden'}
    }
    ent = execute_query(dataset, query).get('result')
    ent_id = ent.get('id')
    avoid = [ent_id] + list(Pairing.existing(dataset, ent_id))
    q = {
        'id|!=': avoid,
        'label%=': ent.get('label'),
        'same_as': {'optional': 'forbidden'},
        '!same_as': {'optional': 'forbidden', 'id': ent_id}
    }
    for res in execute_query(dataset, [q]).get('result'):
        return (res.get('id'), ent_id, res.get('score'))


def generate_best_random_pairing(dataset, num_rounds=10, cutoff=None):
    best_pair = None
    best_score = 0
    pairing = None
    for i in range(num_rounds):
        left_id, right_id, score = generate_random_pairing(dataset)
        if cutoff is not None and score >= cutoff:
            pairing = Pairing.update({
                'left_id': left_id,
                'right_id': right_id
            }, dataset, None, score=score)
            break
        if score > best_score:
            best_score = score
            best_pair = (left_id, right_id)

    if pairing is None and best_pair is not None:
        pairing = Pairing.update({
            'left_id': best_pair[0],
            'right_id': best_pair[1]
        }, dataset, None, score=best_score)

    db.session.commit()
    return pairing


def request_pairing(dataset, num_rounds=10, cutoff=95, exclude=None):
    generate_pairings.delay(dataset.slug)

    q = Pairing.all().filter_by(dataset=dataset)
    q = q.filter_by(decided=False)
    if exclude is not None:
        q = q.filter(~Pairing.id.in_(exclude))
    q = q.order_by(Pairing.score.desc())

    next_ = q.first()
    if next_ is not None:
        return next_

    return generate_best_random_pairing(dataset, num_rounds=num_rounds,
                                        cutoff=cutoff)
