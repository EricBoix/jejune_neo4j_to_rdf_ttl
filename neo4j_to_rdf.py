"""Export a Neo4j database to an RDF file in Turtle format."""

import os
import re
import sys

from dotenv import load_dotenv
from neo4j import GraphDatabase
from rdflib import Graph, Literal, Namespace, RDF, URIRef, XSD


class Neo4jToRdfExporter:
    """Query a Neo4j database and convert its nodes and relationships to RDF."""

    EX = Namespace("http://example.org/graph/")
    NEO = Namespace("http://example.org/neo4j/")
    JEJUNE = Namespace("urn:jejune:")
    EXTRACTED_FROM = NEO["extractedFrom"]
    DOCUMENT_CHUNK = JEJUNE["DocumentChunk"]
    # Not defined in every rdflib release (RDF 1.2 term), hence spelled out
    RDF_REIFIES = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#reifies")

    _SANITIZE_PATTERN = re.compile(r"[^A-Za-z0-9_]")

    def __init__(self, driver) -> None:
        self.driver = driver

    @classmethod
    def _sanitize(cls, element_id: str) -> str:
        """Turn an element ID into a safe URI local name."""
        return cls._SANITIZE_PATTERN.sub("_", element_id)

    @classmethod
    def _node_uri(cls, element_id: str) -> URIRef:
        return cls.EX[f"node_{cls._sanitize(element_id)}"]

    @classmethod
    def _predicate(cls, name: str) -> URIRef:
        return cls.NEO[cls._SANITIZE_PATTERN.sub("_", name)]

    @staticmethod
    def _to_literal(value):
        """Convert a Python value to an rdflib Literal with an XSD type."""
        if isinstance(value, bool):
            return Literal(value, datatype=XSD.boolean)
        if isinstance(value, int):
            return Literal(value, datatype=XSD.integer)
        if isinstance(value, float):
            return Literal(value, datatype=XSD.double)
        return Literal(str(value), datatype=XSD.string)

    @classmethod
    def _add_properties(cls, graph: Graph, subject, props: dict):
        """Add property triples for a subject, expanding lists."""
        for key, value in props.items():
            if value is None:
                continue
            pred = cls._predicate(key)
            if isinstance(value, list):
                for item in value:
                    if item is not None:
                        graph.add((subject, pred, cls._to_literal(item)))
            else:
                graph.add((subject, pred, cls._to_literal(value)))

    @staticmethod
    def _term(node) -> str:
        """Turtle form of an rdflib term (full IRI or typed literal)."""
        return node.n3()

    @classmethod
    def _reifier_turtle(
        cls,
        reifier_label: str,
        src: URIRef,
        pred: URIRef,
        tgt: URIRef,
        chunk_uris,
        props: dict,
    ) -> str:
        """Turtle 1.2 text of a reifier annotating the triple term <<( src pred tgt )>>.

        rdflib cannot serialise triple terms, hence this text is appended to the
        rdflib serialisation. Full IRIs are used so that no prefix is required.
        """
        lines = [
            f"_:{reifier_label} {cls._term(cls.RDF_REIFIES)} "
            f"<<( {cls._term(src)} {cls._term(pred)} {cls._term(tgt)} )>>"
        ]
        for chunk_uri in chunk_uris:
            lines.append(f"{cls._term(cls.EXTRACTED_FROM)} {cls._term(chunk_uri)}")
        for key, value in props.items():
            if value is None:
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                if item is not None:
                    lines.append(
                        f"{cls._term(cls._predicate(key))} "
                        f"{cls._term(cls._to_literal(item))}"
                    )
        return " ;\n    ".join(lines) + " .\n"

    def build_graph(self) -> tuple[Graph, str, dict]:
        """Query Neo4j and return an rdflib Graph, Turtle 1.2 triple-term text and summary counts."""
        graph = Graph()
        graph.bind("ex", self.EX)
        graph.bind("neo", self.NEO)
        graph.bind("jejune", self.JEJUNE)

        node_count = 0
        rel_count = 0
        # Document.id (chunk id, as stored in relationship chunk_ids) -> node URI
        chunk_uri_by_id = {}
        reifier_blocks = []
        unresolved_chunk_count = 0

        with self.driver.session() as session:
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
                uri = self._node_uri(record["eid"])
                # A chunk node is created by langchain_neo4j (include_source=True) as a
                # Document carrying its text. An LLM-extracted entity that happens to be
                # typed Document also carries __Entity__ and no text: it is no chunk.
                is_chunk = (
                    "Document" in record["labels"]
                    and "__Entity__" not in record["labels"]
                    and "text" in record["props"]
                )
                for label in record["labels"]:
                    if is_chunk and label == "Document":
                        graph.add((uri, RDF.type, self.DOCUMENT_CHUNK))
                    else:
                        graph.add(
                            (
                                uri,
                                RDF.type,
                                self.NEO[self._SANITIZE_PATTERN.sub("_", label)],
                            )
                        )
                self._add_properties(graph, uri, record["props"])
                if is_chunk and "id" in record["props"]:
                    chunk_uri_by_id[record["props"]["id"]] = uri

            # --- Relationships ---
            result = session.run(
                "MATCH (a)-[r]->(b) "
                "RETURN elementId(a) AS src, elementId(b) AS tgt, "
                "type(r) AS rtype, properties(r) AS props"
            )
            for record in result:
                rel_count += 1
                src = self._node_uri(record["src"])
                tgt = self._node_uri(record["tgt"])
                pred = self._predicate(record["rtype"])
                props = record["props"]

                # Always emit the direct triple
                graph.add((src, pred, tgt))

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
                        self._reifier_turtle(
                            f"reifier_{rel_count}",
                            src,
                            pred,
                            tgt,
                            chunk_uris,
                            other_props,
                        )
                    )

        if unresolved_chunk_count:
            print(
                f"Warning: {unresolved_chunk_count} chunk_ids matched no DocumentChunk node",
                file=sys.stderr,
            )

        return (
            graph,
            "".join(reifier_blocks),
            {
                "nodes": node_count,
                "relationships": rel_count,
                "triples": len(graph),
                "triple_terms": len(reifier_blocks),
            },
        )

    def export(self, output_path: str) -> None:
        """Write the Turtle serialisation followed by the reifiers to output_path."""
        graph, triple_term_turtle, summary = self.build_graph()
        turtle_text = graph.serialize(format="turtle")
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

    @classmethod
    def main(cls) -> None:
        load_dotenv()

        neo4j_uri = os.environ.get("NEO4J_URI")
        username = os.environ.get("NEO4J_USERNAME")
        password = os.environ.get("NEO4J_PASSWORD")

        missing = [
            variable_name
            for variable_name, variable_value in [
                ("NEO4J_URI", neo4j_uri),
                ("NEO4J_USERNAME", username),
                ("NEO4J_PASSWORD", password),
            ]
            if not variable_value
        ]
        if missing:
            sys.exit(f"Missing environment variables: {', '.join(missing)}")

        output_path = sys.argv[1] if len(sys.argv) > 1 else "output.ttl"

        driver = GraphDatabase.driver(neo4j_uri, auth=(username, password))
        try:
            driver.verify_connectivity()
            print(f"Connected to {neo4j_uri}")
            cls(driver).export(output_path)
        finally:
            driver.close()


if __name__ == "__main__":
    Neo4jToRdfExporter.main()
