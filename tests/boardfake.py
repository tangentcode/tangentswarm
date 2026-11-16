"""A fake gql for tangentswarm.boardcache: answers the batched snapshot query."""


def answer(nodes):
    """The boardcache.query answer for these item nodes (split into the prs and issues parts)."""
    def part(kind):
        return {'nodes': [n for n in nodes if (n.get('content') or {}).get('__typename') == kind],
                'pageInfo': {'hasNextPage': False, 'endCursor': None}}
    return {'organization': {'projectV2': {'prs': part('PullRequest'), 'issues': part('Issue')}}}


def gql(nodes, calls=None):
    def f(q, **v):
        if calls is not None:
            calls.append(q)
        return answer(nodes)
    return f
