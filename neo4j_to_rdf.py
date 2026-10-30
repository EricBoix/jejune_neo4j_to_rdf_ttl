"""Export a Neo4j database to an RDF file in Turtle format."""

import os
import re
import sys

from dotenv import load_dotenv
from neo4j import GraphDatabase
from rdflib import Graph, Literal, Namespace, RDF, URIRef, XSD


EX = Namespace("http://example.org/graph/")
NEO = Namespace("http://example.org/neo4j/")
EXTRACTED_FROM = NEO["extractedFrom"]
# Not defined in every rdflib release (RDF 1.2 term), hence spelled out
RDF_REIFIES = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#reifies")

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_]")


def _sanitize(element_id: str) -> str:
    """Turn an element ID into a safe URI local name."""
    return _SANITIZE_RE.sub("_", element_id)


def _node_uri(element_id: str) -> URIRef:
    return EX[f"node_{_sanitize(element_id)}"]


def _predicate(name: str) -> URIRef:
    return NEO[_SANITIZE_RE.sub("_", name)]


def _to_literal(value):
    """Convert a Python value to an rdflib Literal with an XSD type."""
    if isinstance(value, bool):
        return Literal(value, datatype=XSD.boolean)
    if isinstance(value, int):
        return Literal(value, datatype=XSD.integer)
    if isinstance(value, float):
        return Literal(value, datatype=XSD.double)
    return Literal(str(value), datatype=XSD.string)


def _add_properties(g: Graph, subject, props: dict):
    """Add property triples for a subject, expanding lists."""
    for key, value in props.items():
        if value is None:
            continue
        pred = _predicate(key)
        if isinstance(value, list):
            for item in value:
                if item is not None:
                    g.add((subject, pred, _to_literal(item)))
        else:
            g.add((subject, pred, _to_literal(value)))


def _term(node) -> str:
    """Turtle form of an rdflib term (full IRI or typed literal)."""
    return node.n3()


def _reifier_turtle(
    reifier_label: str, src: URIRef, pred: URIRef, tgt: URIRef, chunk_uris, props: dict
) -> str:
    """Turtle 1.2 text of a reifier annotating the triple term <<( src pred tgt )>>.

    rdflib cannot serialise triple terms, hence this text is appended to the
    rdflib serialisation. Full IRIs are used so that no prefix is required.
    """
    lines = [
        f"_:{reifier_label} {_term(RDF_REIFIES)} "
        f"<<( {_term(src)} {_term(pred)} {_term(tgt)} )>>"
    ]
    for chunk_uri in chunk_uris:
        lines.append(f"{_term(EXTRACTED_FROM)} {_term(chunk_uri)}")
    for key, value in props.items():
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item is not None:
                lines.append(f"{_term(_predicate(key))} {_term(_to_literal(item))}")
    return " ;\n    ".join(lines) + " .\n"


def build_graph(driver) -> tuple[Graph, str, dict]:
    """Query Neo4j and return an rdflib Graph, Turtle 1.2 triple-term text and summary counts."""
    g = Graph()
    g.bind("ex", EX)
    g.bind("neo", NEO)

    node_count = 0
    rel_count = 0
    # Document.id (chunk id, as stored in relationship chunk_ids) -> node URI
    chunk_uri_by_id = {}
    reifier_blocks = []
    unresolved_chunk_count = 0

    with driver.session() as session:
        # --- Nodes ---
        result = session.run(
            "MATCH (n) RETURN elementId(n) AS eid, labels(n) AS labels, properties(n) AS props"
        )
        for record in result:
            node_count += 1
            # The embedding property of a node is a lengthy vector that we
            # can later retrieve in the Neo4j DB
            if "embedding" in record["props"].keys():
                del record["props"]["embedding"]
            uri = _node_uri(record["eid"])
            for label in record["labels"]:
                g.add((uri, RDF.type, NEO[_SANITIZE_RE.sub("_", label)]))
            _add_properties(g, uri, record["props"])
            if "Document" in record["labels"] and "id" in record["props"]:
                chunk_uri_by_id[record["props"]["id"]] = uri

        # --- Relationships ---
        result = session.run(
            "MATCH (a)-[r]->(b) "
            "RETURN elementId(a) AS src, elementId(b) AS tgt, "
            "type(r) AS rtype, properties(r) AS props"
        )
        for record in result:
            rel_count += 1
            src = _node_uri(record["src"])
            tgt = _node_uri(record["tgt"])
            pred = _predicate(record["rtype"])
            props = record["props"]

            # Always emit the direct triple
            g.add((src, pred, tgt))

            # Reify when the relationship carries properties (RDF 1.2 triple
            # term annotated by a reifier, replacing the former rdf:Statement).
            # The chunk_ids property becomes one neo:extractedFrom link per chunk.
            if props:
                chunk_uris = []
                for chunk_id in props.get("chunk_ids", []):
                    if chunk_id in chunk_uri_by_id:
                        chunk_uris.append(chunk_uri_by_id[chunk_id])
                    else:
                        unresolved_chunk_count += 1
                other_props = {
                    key: value for key, value in props.items() if key != "chunk_ids"
                }
                reifier_blocks.append(
                    _reifier_turtle(
                        f"reifier_{rel_count}", src, pred, tgt, chunk_uris, other_props
                    )
                )

    if unresolved_chunk_count:
        print(
            f"Warning: {unresolved_chunk_count} chunk_ids matched no Document node",
            file=sys.stderr,
        )

    return (
        g,
        "".join(reifier_blocks),
        {
            "nodes": node_count,
            "relationships": rel_count,
            "triples": len(g),
            "triple_terms": len(reifier_blocks),
        },
    )


def main():
    load_dotenv()

    uri = os.environ.get("NEO4J_URI")
    username = os.environ.get("NEO4J_USERNAME")
    password = os.environ.get("NEO4J_PASSWORD")

    missing = [
        v
        for v, val in [
            ("NEO4J_URI", uri),
            ("NEO4J_USERNAME", username),
            ("NEO4J_PASSWORD", password),
        ]
        if not val
    ]
    if missing:
        sys.exit(f"Missing environment variables: {', '.join(missing)}")

    output_path = sys.argv[1] if len(sys.argv) > 1 else "output.ttl"

    driver = GraphDatabase.driver(uri, auth=(username, password))
    try:
        driver.verify_connectivity()
        print(f"Connected to {uri}")

        g, triple_term_turtle, summary = build_graph(driver)
        turtle_text = g.serialize(format="turtle")
        with open(output_path, "w", encoding="utf-8") as output_file:
            output_file.write(turtle_text)
            if triple_term_turtle:
                output_file.write("\n# RDF 1.2 triple terms (reifiers)\n")
                output_file.write(triple_term_turtle)

        print(
            f"Exported {summary['nodes']} nodes, "
            f"{summary['relationships']} relationships "
            f"({summary['triples']} triples, "
            f"{summary['triple_terms']} triple-term reifiers) to {output_path}"
        )
    finally:
        driver.close()


if __name__ == "__main__":
    main()
