function compareStrings(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

export function generationFromVectorId(id) {
  const match = String(id).match(/^(.+):(ontologies|software):(Q\d+)$/);
  return match ? match[1] : null;
}

export function obsoleteGenerationGroups(vectorIds, retainedGenerations) {
  const retained = new Set(retainedGenerations);
  const groups = new Map();
  for (const id of vectorIds) {
    const generationId = generationFromVectorId(id);
    if (!generationId || retained.has(generationId)) continue;
    if (!groups.has(generationId)) groups.set(generationId, []);
    groups.get(generationId).push(id);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => compareStrings(a, b))
    .map(([generationId, ids]) => ({ generationId, ids: ids.sort(compareStrings) }));
}

export async function retireObsoleteGenerations({
  vectorIds,
  retainedGenerations,
  batchSize = 1000,
  markStatus,
  deleteBatch,
  waitUntilAbsent,
}) {
  const groups = obsoleteGenerationGroups(vectorIds, retainedGenerations);
  const retired = [];

  for (const group of groups) {
    const mutationIds = [];
    await markStatus({
      generationId: group.generationId,
      status: "retiring",
      mutationIds,
      failureReason: null,
    });
    try {
      for (let offset = 0; offset < group.ids.length; offset += batchSize) {
        const mutationId = await deleteBatch(group.ids.slice(offset, offset + batchSize));
        if (mutationId) mutationIds.push(mutationId);
      }
      await markStatus({
        generationId: group.generationId,
        status: "retiring",
        mutationIds,
        failureReason: null,
      });
      await waitUntilAbsent(group.ids);
      await markStatus({
        generationId: group.generationId,
        status: "retired",
        mutationIds,
        failureReason: null,
      });
      retired.push({ ...group, mutationIds });
    } catch (error) {
      await markStatus({
        generationId: group.generationId,
        status: "retiring",
        mutationIds,
        failureReason: error instanceof Error ? error.message : String(error),
      });
      throw error;
    }
  }

  return retired;
}
