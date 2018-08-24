import attr
import struct
import msprime
import json
from collections import OrderedDict
import warnings

from .slim_metadata import *
from .provenance import *

SLIM_VERSION = "3.0"
SLIM_FILE_VERSION = "0.1"

def load(path, slim_format):
    '''
    Load the tree sequence found in the .trees file at ``path``. If the .trees
    file is SLiM-compatible, set ``slim_format`` to ``True`` (in which case
    this returns a :class:`SlimTreeSequence`); otherwise, this just calls
    :meth:`msprime.load`.  A SlimTreeSequence has all node and migration times
    in the tree sequence shifted relative to that recorded in the file by the
    current generation recorded by SLiM so that the tskit times are measured in
    units of generations before the end of the simulation.

    :param string path: The path to a .trees file.
    :param bool slim_format: Whether the .trees file should be coverted from
        SLiM format.
    '''
    if slim_format:
        ts = SlimTreeSequence.load(path)
    else:
        ts = msprime.load(path)
    return ts


def load_tables(tables, slim_format):
    '''
    See :func:`load`.

    :param TableCollection tables: A set of tables.
    :param bool slim_format: Whether the tables should be coverted from
        SLiM format.
    '''
    if slim_format:
        ts = SlimTreeSequence.load_tables(tables)
    else:
        ts = tables.tree_sequence()
    return ts


def mutate(ts, *args, **kwargs):
    '''
    Mutate the tree sequence. This is a wrapper around
    :meth:``msprime.mutate`` that retains the SLiM-specific information.

    See :meth:``msprime.TreeSequence.mutate`` for arguments.
    '''
    mts = msprime.mutate(ts, *args, **kwargs)
    tables = mts.dump_tables()
    mut_ts = SlimTreeSequence.load_tables(tables)
    return mut_ts


def annotate_defaults(ts, model_type, slim_generation):
    '''
    Takes a tree sequence (as produced by msprime, for instance), and adds in the
    information necessary for SLiM to use it as an initial state, filling in
    mostly default values. Returns a :class:`SlimTreeSequence`.

    :param TreeSequence ts: A :class:`TreeSequence`.
    :param string model_type: SLiM model type: either "WF" or "nonWF".
    :param int slim_generation: What generation number in SLiM correponds to
        ``time=0`` in the tree sequence.
    '''
    tables = ts.dump_tables()
    annotate_defaults_tables(tables, model_type, slim_generation)
    return SlimTreeSequence.load_tables(tables)


def annotate_defaults_tables(tables, model_type, slim_generation):
    '''
    Does the work of :func:`annotate_defaults()`, but modifies the tables in place: so,
    takes tables as produced by ``msprime``, and makes them look like the
    tables as output by SLiM. See :func:`annotate_defaults` for details.
    '''
    if (type(slim_generation) is not int) or (slim_generation < 1):
        raise ValueError("SLiM generation must be an integer and at least 1.")
    # set_nodes must come before set_populations
    if model_type == "WF":
        default_ages = -1
    elif model_type == "nonWF":
        default_ages = 0
    else:
        raise ValueError("Model type must be 'WF' or 'nonWF'")
    _set_nodes_individuals(tables, age=default_ages)
    _set_populations(tables)
    _set_sites_mutations(tables)
    _set_provenance(tables, model_type=model_type, slim_generation=slim_generation)


class SlimTreeSequence(msprime.TreeSequence):
    '''
    This is just like a :class:`TreeSequence`, except that:
        - Times are shifted by the `generation` in the last SLiM entry
            of the Provenance table.
    You should create a :class:`SlimTreeSequence` using one of
    :meth:`SlimTreeSequence.load_tables` :meth:`SlimTreeSequence.load`,
    :func:`load`, or :func:`load_tables`.

    :ivar slim_generation: The amount by which times have been shifted.
    :vartype slim_generation: int
    '''

    def __init__(self, ts, slim_generation):
        self._ll_tree_sequence = ts._ll_tree_sequence
        self.slim_generation = slim_generation


    @classmethod
    def load(cls, path):
        '''
        Load a :class:`SlimTreeSequence` from a .trees file on disk.

        :param string path: The path to a .trees file.
        :rtype SlimTreeSequence:
        '''
        # roundabout way to load just the tables
        ts = msprime.load(path)
        tables = ts.dump_tables()
        provenance = get_provenance(tables)
        _set_slim_generation(tables, provenance.slim_generation)
        return cls.load_tables(tables)

    @classmethod
    def load_tables(cls, tables):
        '''
        Creates the :class:`SlimTreeSequence` defined by the tables.

        :param TableCollection tables: A set of tables, as produced by SLiM
            or by annotate_defaults().
        :rtype SlimTreeSequence:
        '''
        # a roundabout way to copy the tables
        ts = tables.tree_sequence()
        new_tables = ts.dump_tables()
        provenance = get_provenance(new_tables)
        ts = new_tables.tree_sequence()
        return cls(ts, provenance.slim_generation)

    def dump(self, path, **kwargs):
        '''
        Write out the .trees file that can be read back in by SLiM. See
        :meth:`msprime.TreeSequence.dump()` for other arguments.

        :param string path: The path to a .trees file.
        '''
        # This would be simpler if there were a python-level TableCollection.dump
        # method: https://github.com/tskit-dev/msprime/issues/547
        tables = self.dump_tables()
        _set_slim_generation(tables, -1 * self.slim_generation)
        temp_ts = tables.tree_sequence()
        msprime.TreeSequence.dump(temp_ts, path, **kwargs)

    def simplify(self, samples, **kwargs):
        '''
        Simplify the tree sequence. This is a wrapper around
        :meth:``TreeSequence.simplify`` that retains the SLiM-specific information.

        See :meth:``msprime.TreeSequence.simplify`` for arguments.
        '''
        tables = self.dump_tables()
        tables.simplify(samples, **kwargs)
        ts = SlimTreeSequence.load_tables(tables)
        return ts

    def recapitate(self, recombination_rate, keep_first_generation=False,
                   population_configurations=None, **kwargs):
        '''
        Returns a "recapitated" tree sequence, by using msprime to run a
        coalescent simulation from the "top" of this tree sequence, i.e.,
        allowing any uncoalesced lineages to coalesce. For this procedure to
        work, you must have recorded in the tree sequence the initial generation
        (i.e., "remembered" those individuals in SLiM). However, if you are not
        interested in the genotypes of the initial generation, you may remove
        these at this point (which makes the work of recapitation substantially
        less). If you wish to keep the history of the first generation as well,
        set ``keep_first_generation`` to ``True``.

        Note that ``Ne`` is not set automatically, so defaults to ``1.0``; you probably
        want to set it explicitly.  Similarly, migration is not set up
        automatically, so that if there are uncoalesced lineages in more than
        one population, you will need to pass in a migration matrix to allow
        coalescence. In both cases, remember that population IDs in ``tskit`` begin
        with 0, so that if your SLiM simulation has populations ``p1`` and ``p2``,
        then the tree sequence will have three populations (but with no nodes
        assigned to population 0), so that migration rate of 1.0 between ``p1`` and
        ``p2`` needs a migration matrix of
           [[0.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 0.0]]

        :param float recombination_rate: The recombination rate - only a constant 
            recombination rate is allowed.
        :param bool keep_first_generation: Whether to keep the individuals (and genomes)
            corresponding to the first SLiM generation in the resulting tree sequence.
        :param list population_configurations: See :meth:`msprime.simulate()` for
            this argument; if not provided, each population will have zero growth rate
            and the same effective population size.
        :param dict kwargs: Any other arguments to :meth:`msprime.simulate()`.
        '''
        recomb = msprime.RecombinationMap(positions = [0.0, self.sequence_length], 
                                          rates = [recombination_rate, 0.0],
                                          num_loci = int(self.sequence_length))

        if population_configurations is None:
            population_configurations = [msprime.PopulationConfiguration() 
                                         for _ in range(self.num_populations)]

        if not keep_first_generation:
            tables = self.dump_tables()
            flags = tables.nodes.flags
            first_gen_nodes = (tables.nodes.time == self.slim_generation)
            if sum(first_gen_nodes) == 0:
                warnings.warn("Tree sequence does not have the initial generation" +
                              " marked as samples; are you sure the result will be" +
                              " correct?")
            flags[first_gen_nodes] = 0
            tables.nodes.set_columns(flags=flags, population=tables.nodes.population,
                    individual=tables.nodes.individual, time=tables.nodes.time,
                    metadata=tables.nodes.metadata, 
                    metadata_offset=tables.nodes.metadata_offset)
            ts = load_tables(tables, slim_format=True)
        else:
            ts = self

        recap = msprime.simulate(
                    from_ts = ts, 
                    population_configurations = population_configurations,
                    recombination_map = recomb,
                    start_time = self.slim_generation,
                    **kwargs)
        ts = SlimTreeSequence.load_tables(recap.tables)
        return ts


def _set_slim_generation(tables, slim_generation):
    '''
    Modifying ``tables`` in place, shifts the "time ago" entries in the tables
    to be measured in units of time *before* `slim_generation`, by adding
    ``slim_generation`` to the ``time`` columns of Node and Migration tables.
    Can be inverted by passing in ``-1 * slim_generation``.
    '''
    tables.nodes.set_columns(flags=tables.nodes.flags,
            time=tables.nodes.time + slim_generation,
            population=tables.nodes.population, individual=tables.nodes.individual,
            metadata=tables.nodes.metadata, metadata_offset=tables.nodes.metadata_offset)
    tables.migrations.set_columns(left=tables.migrations.left, right=tables.migrations.right,
            node=tables.migrations.node, source=tables.migrations.source,
            dest=tables.migrations.dest, time=tables.migrations.time + slim_generation)


def _set_nodes_individuals(
        tables, node_ind=None, location=(0, 0, 0), age=0, ind_id=None,
        ind_population=None, ind_sex=INDIVIDUAL_TYPE_HERMAPHRODITE,
        ind_flags=0, slim_ind_flags=0, node_id=None,
        node_is_null=False, node_type=GENOME_TYPE_AUTOSOME):
    '''
    Adds to a TableCollection the information relevant to individuals required
    for SLiM to load in a tree sequence, that is found in Node and Individual
    tables.  This will replace any existing Individual table, and will replace
    any information already in the individual, metadata, and population columns
    of the Node table.

    This is designed to make it easy to assign default values:
    - (node_ind) the 2*j-th and (2*j+1)-st `sample` nodes to individual j
    - (location) individual locations to (0, 0, 0)
    - (age) individual age to 0
    - (ind_id) SLiM individual pedigree IDs to sequential integers starting from 0
    - (ind_population) individual populations to 0
    - (node_id) SLiM genome IDs to sequential integers starting with samples from 0
    - (node_is_null) genomes to be non-null
    - (node_type) genome type to 0 (= autosome)
    '''
    samples = list(filter(lambda j: tables.nodes.flags[j] & msprime.NODE_IS_SAMPLE,
                          range(tables.nodes.num_rows)))
    if (len(samples) % 2) != 0:
        raise ValueError("There must be an even number of sampled nodes,"\
                         + "since organisms are diploid.")

    if node_ind is None:
        node_ind = [msprime.NULL_INDIVIDUAL for _ in range(tables.nodes.num_rows)]
        for j, k in enumerate(samples):
            node_ind[j] = int(k/2)

    num_individuals = max(node_ind) + 1
    num_nodes = tables.nodes.num_rows

    if type(location) is tuple:
        location = [location for _ in range(num_individuals)]
    assert(len(location) == num_individuals)

    if type(age) is int or type(age) is float:
        age = [age for _ in range(num_individuals)]
    assert(len(age) == num_individuals)

    if ind_id is None:
        ind_id = list(range(num_individuals))
    assert(len(ind_id) == num_individuals)

    if type(ind_sex) is int:
        ind_sex = [ind_sex for _ in range(num_individuals)]
    assert(len(ind_sex) == num_individuals)

    if type(slim_ind_flags) is int:
        slim_ind_flags = [slim_ind_flags for _ in range(num_individuals)]
    assert(len(slim_ind_flags) == num_individuals)

    if type(ind_flags) is int:
        ind_flags = [ind_flags for _ in range(num_individuals)]
    assert(len(ind_flags) == num_individuals)

    if node_id is None:
        node_id = [-1 for _ in range(num_nodes)]
        for j, k in enumerate(list(samples)
                              + sorted(list(set(range(num_nodes))
                                            - set(samples)))):
            node_id[k] = j
    assert(len(node_id) == num_nodes)

    if type(node_is_null) is bool:
        node_is_null = [node_is_null for _ in range(num_nodes)]
    assert(len(node_is_null) == num_nodes)

    if type(node_type) is int:
        node_type = [node_type for _ in range(num_nodes)]
    assert(len(node_type) == tables.nodes.num_rows)

    if ind_population is None:
        # set the individual populations based on what's in the nodes
        ind_population = [msprime.NULL_POPULATION for _ in range(num_individuals)]
        for j, u in enumerate(node_ind):
            ind_population[u] = tables.nodes.population[j]
    assert(len(ind_population) == num_individuals)

    # check for consistency: every individual has two nodes, and populations agree
    ploidy = [0 for _ in range(num_individuals)]
    for j in samples:
        u = node_ind[j]
        assert(u >= 0)
        ploidy[u] += 1
        if tables.nodes.population[j] != ind_population[u]:
            raise ValueError("Inconsistent populations: nodes and individuals do not agree.")

    if any([p != 2 for p in ploidy]):
        raise ValueError("Not all individuals have two assigned nodes.")

    tables.nodes.set_columns(flags=tables.nodes.flags, time=tables.nodes.time,
                             population=tables.nodes.population, individual=node_ind,
                             metadata=tables.nodes.metadata,
                             metadata_offset=tables.nodes.metadata_offset)

    loc_vec, loc_off = msprime.pack_bytes(location)
    tables.individuals.set_columns(
            flags=ind_flags, location=loc_vec, location_offset=loc_off)

    individual_metadata = [IndividualMetadata(*x) for x in
                           zip(ind_id, age, ind_population, ind_sex, slim_ind_flags)]
    node_metadata = [None for _ in range(num_nodes)]
    for j in samples:
        node_metadata[j] = NodeMetadata(slim_id=node_id[j], is_null=node_is_null[j],
                                        genome_type=node_type[j])

    annotate_individual_metadata(tables, individual_metadata)
    annotate_node_metadata(tables, node_metadata)


def _set_populations(
        tables, pop_id=None, selfing_fraction=0.0, female_cloning_fraction=0.0,
        male_cloning_fraction=0.0, sex_ratio=0.5, bounds_x0=0.0, bounds_x1=0.0,
        bounds_y0=0.0, bounds_y1=0.0, bounds_z0=0.0, bounds_z1=0.0,
        migration_records=None):
    '''
    Adds to a TableCollection the information about populations required for SLiM
    to load a tree sequence. This will replace anything already in the Population
    table.
    '''
    num_pops = max(tables.nodes.population) + 1
    for md in msprime.unpack_bytes(tables.individuals.metadata,
                                   tables.individuals.metadata_offset):
        try:
            ind_md = decode_individual(md)
        except:
            raise ValueError("Individuals do not have metadata:"
                    + "need to run set_nodes_individuals() first?")
        assert(ind_md.population < num_pops)
    if pop_id is None:
        pop_id = list(range(num_pops))
    assert(len(pop_id) == num_pops)

    if type(selfing_fraction) is float:
        selfing_fraction = [selfing_fraction for _ in range(num_pops)]
    assert(len(selfing_fraction) == num_pops)

    if type(female_cloning_fraction) is float:
        female_cloning_fraction = [female_cloning_fraction for _ in range(num_pops)]
    assert(len(female_cloning_fraction) == num_pops)

    if type(male_cloning_fraction) is float:
        male_cloning_fraction = [male_cloning_fraction for _ in range(num_pops)]
    assert(len(male_cloning_fraction) == num_pops)

    if type(sex_ratio) is float:
        sex_ratio = [sex_ratio for _ in range(num_pops)]
    assert(len(sex_ratio) == num_pops)

    if type(bounds_x0) is float:
        bounds_x0 = [bounds_x0 for _ in range(num_pops)]
    assert(len(bounds_x0) == num_pops)

    if type(bounds_x1) is float:
        bounds_x1 = [bounds_x1 for _ in range(num_pops)]
    assert(len(bounds_x1) == num_pops)

    if type(bounds_y0) is float:
        bounds_y0 = [bounds_y0 for _ in range(num_pops)]
    assert(len(bounds_y0) == num_pops)

    if type(bounds_y1) is float:
        bounds_y1 = [bounds_y1 for _ in range(num_pops)]
    assert(len(bounds_y1) == num_pops)

    if type(bounds_z0) is float:
        bounds_z0 = [bounds_z0 for _ in range(num_pops)]
    assert(len(bounds_z0) == num_pops)

    if type(bounds_z1) is float:
        bounds_z1 = [bounds_z1 for _ in range(num_pops)]
    assert(len(bounds_z1) == num_pops)

    if migration_records is None:
        migration_records = [[] for _ in range(num_pops)]
    assert(len(migration_records) == num_pops)
    for mrl in migration_records:
        for mr in mrl:
            assert(type(mr) is PopulationMigrationMetadata)

    population_metadata = [PopulationMetadata(*x) for x in
                           zip(pop_id, selfing_fraction, female_cloning_fraction,
                               male_cloning_fraction, sex_ratio, bounds_x0,
                               bounds_x1, bounds_y0, bounds_y1, bounds_z0, bounds_z1,
                               migration_records)]
    annotate_population_metadata(tables, population_metadata)


def _set_sites_mutations(
        tables, mutation_id=None, mutation_type=1, selection_coeff=0.0,
        population=msprime.NULL_POPULATION, slim_time=None):
    '''
    Adds to a TableCollection the information relevant to mutations required
    for SLiM to load in a tree sequence. This means adding to the metadata column
    of the Mutation table,  It will also
    - give SLiM IDs to each mutation
    - round Site positions to integer values
    - stack any mutations that end up at the same position as a result
    - replace ancestral states with ""
    This will replace any information already in the metadata or derived state
    columns of the Mutation table.
    '''
    num_mutations = tables.mutations.num_rows

    if mutation_id is None:
        mutation_id = list(range(num_mutations))
    assert(len(mutation_id) == num_mutations)

    if type(mutation_type) is int:
        mutation_type = [mutation_type for _ in range(num_mutations)]
    assert(len(mutation_type) == num_mutations)

    if type(selection_coeff) is float:
        selection_coeff = [selection_coeff for _ in range(num_mutations)]
    assert(len(selection_coeff) == num_mutations)

    if type(population) is int:
        population = [population for _ in range(num_mutations)]
    assert(len(population) == num_mutations)

    if slim_time is None:
        ## This may *not* make sense because we have to round:
        # slim_time = [(-1) * int(tables.nodes.time[u]) for u in tables.mutations.node]
        slim_time = [0 for _ in range(num_mutations)]
    assert(len(slim_time) == num_mutations)

    mutation_metadata = [[MutationMetadata(*x)] for x in
                         zip(mutation_type, selection_coeff, population, slim_time)]
    annotate_mutation_metadata(tables, mutation_metadata)

#######
# Provenance
####################
# See provenances.py for the structure of a Provenance entry.


@attr.s
class ProvenanceMetadata(object):
    model_type = attr.ib()
    slim_generation = attr.ib()


def get_provenance(tables):
    '''
    Extracts model type, slim generation, and remembmered node count from the last
    entry in the provenance table that is tagged with "program"="SLiM".

    :param TableCollection tables: The tables.
    :rtype ProvenanceMetadata:
    '''
    prov = [json.loads(x.record) for x in tables.provenances]
    slim_prov = [u for u in prov if ('software' in u 
                                     and 'name' in u['software']
                                     and u['software']['name'] == "SLiM")]
    if len(slim_prov) == 0:
        raise ValueError("Tree sequence contains no SLiM provenance entries.")
    last_slim_prov = slim_prov[len(slim_prov)-1]['slim']
    return ProvenanceMetadata(last_slim_prov["model_type"], last_slim_prov["generation"])


def _set_provenance(tables, model_type, slim_generation):
    '''
    Appends to the provenance table of a :class:`TableCollection` a record containing
    the information that SLiM expects to find there.

    :param TableCollection tables: The table collection.
    :param string model_type: The model type: either "WF" or "nonWF".
    :param int slim_generation: The "current" generation in the SLiM simulation.
    '''
    pyslim_dict = get_provenance_dict()
    slim_dict = make_slim_dict(model_type, slim_generation)
    tables.provenances.add_row(json.dumps(pyslim_dict))
    tables.provenances.add_row(json.dumps(slim_dict))

