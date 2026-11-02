# Neo4j to RDF file exportation utility

## Purpose

The purpose of this directory is to implement a Neo4j database exportation code that

- logs into a neo4j database (using the authentication info provided in a `.env`file)
- collects all the nodes and edges
- exports this content to a newly created RDF file (using the Turtle format)
- can be easily customized (node/edge filtering) and thus written in Python

## Setup

```bash
# Prepare the virtual environment
python3.10 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

or equivalently run `make setup` and `source venv/bin/activate`

```bash
cp .env.example .env
```

and edit the resulting `.env` file to set your Neo4j credentials e.g.

```bash
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your_password
```

## Usage

Launch a live instance of Neo4j database server: refer e.g. to [https://github.com/EricBoix/jj_neo4j_docker](https://github.com/EricBoix/jj_neo4j_docker).

Then use

```bash
python neo4j_to_rdf.py              # Default output written to `output.ttl`
python neo4j_to_rdf.py graph.ttl    # Custom filename given as output path
```

## Running with Docker

Build the image from the repository root:

```bash
docker build -t jejuneness:jj_neo4j_to_rdf_ttl https://github.com/EricBoix/jj_neo4j_to_rdf_ttl.git#:DockerContext
```

Run the extraction (adjust paths and `.env` as needed):

```bash
docker run --rm \
  --network host \
  -v `pwd`/result_data:/output \
  --env-file .env \
  jejuneness:jj_neo4j_to_rdf_ttl \
  neo4j_to_rdf.py /output/graph.ttl
```

## RDF Mapping

- **Namespaces**: `ex:` = `http://example.org/graph/`,
  `neo:` = `http://example.org/neo4j/`,
  `jejune:` = `urn:jejune:`
- **Nodes**: `ex:node_{id}` with `rdf:type neo:Label` and property literals
- **Chunk nodes**: the Neo4j `Document` nodes holding a `text` (and no
  `__Entity__` label) are typed `jejune:DocumentChunk` instead of `neo:Document`.
  An LLM-extracted entity typed `Document` remains `neo:Document`
- **Relationships without properties**: direct triple
  `ex:node_src neo:REL_TYPE ex:node_tgt`
- **Relationships with properties**: direct triple plus an RDF 1.2 reifier
  (`_:reifier rdf:reifies <<( ex:node_src neo:REL_TYPE ex:node_tgt )>>`)
  carrying the property literals
- **Relationship `chunk_ids`**: one `neo:extractedFrom ex:node_{chunk}` per
  chunk on the reifier, `chunk` being the `jejune:DocumentChunk` node of that id
- **Output**: the rdflib Turtle serialisation, followed by the reifiers
  written as Turtle 1.2 text (rdflib cannot serialise triple terms)

## Devel debug notes

```bash

```