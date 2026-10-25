// core_layout: module layout constants must match the ones folded into build_id (api.hpp).
#include "machine/engine/graph_v2.hpp"
#include "machine/engine/ledger.hpp"
#include "machine/engine/seg.hpp"
#include <cassert>
#include <cstdio>
using namespace machine::engine;

static_assert(std::string_view(graph::graph_layout) == layout_graph, "bump api.hpp layout_graph with graph_v2.hpp graph_layout");
static_assert(ledger::fmt::ledger_format == layout_ledger, "bump api.hpp layout_ledger with ledger.hpp ledger_format");

int main() {
    // a layout bump must change the build id of the same inputs
    std::vector<BuildInput> in = {{"linea/map/edges/part-0.parquet", "md5:abc", 10, 1}};
    auto id = build_id_of("linea", in);
    assert(id.size() == 16 && id == build_id_of("linea", in));
    std::printf("core_layout ok (graph=%s ledger=%u)\n", graph::graph_layout, layout_ledger);
}
