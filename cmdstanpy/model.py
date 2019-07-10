import os
import subprocess
import tempfile

from multiprocessing import cpu_count
from multiprocessing.pool import ThreadPool
from pathlib import Path
from typing import Dict, List, Union

from cmdstanpy import TMPDIR
from cmdstanpy.cmdstan_args import CmdStanArgs, SamplerArgs
from cmdstanpy.stanfit import StanFit
from cmdstanpy.utils import jsondump, do_command, EXTENSION, cmdstan_path


class Model(object):
    """
    Stan model.
    Stores pathnames to Stan program as well as compiled executable.
    Provides functions to compile the model and perform inference on the
    model given data.
    """

    def __init__(self, stan_file: str = None, exe_file: str = None) -> None:
        """Initialize object."""
        self._stan_file = stan_file
        if stan_file is None:
            raise ValueError('must specify Stan program file')
        if not os.path.exists(stan_file):
            raise ValueError('no such file {}'.format(self._stan_file))
        _, filename = os.path.split(stan_file)
        if len(filename) < 6 or not filename.endswith('.stan'):
            raise ValueError('invalid stan filename {}'.format(self._stan_file))
        self._name, _ = os.path.splitext(filename)
        self._exe_file = None
        if exe_file is not None:
            if not os.path.exists(exe_file):
                raise ValueError('no such file {}'.format(self._exe_file))
            _, exename = os.path.split(exe_file)
            if self._name != ''.join([exename, EXTENSION]):
                raise ValueError(
                    'name mismatch between Stan file and compiled'
                    ' executable, expecting basename: {}'
                    ' found: {}'.format(self._name, exename)
                )
            self._exe_file = exe_file

    def __repr__(self) -> str:
        return 'Model(name={},  stan_file="{}", exe_file="{}")'.format(
            self._name, self._stan_file, self._exe_file
        )

    def code(self) -> str:
        """Return Stan program as a string."""
        code = None
        try:
            with open(self._stan_file, 'r') as fd:
                code = fd.read()
        except IOError:
            print('Cannot read file Stan file: {}'.format(self._stan_file))
        return code

    @property
    def name(self) -> str:
        return self._name

    @property
    def stan_file(self) -> str:
        return self._stan_file

    @property
    def exe_file(self) -> str:
        return self._exe_file

    def compile(
        self,
        opt_lvl: int = 2,
        overwrite: bool = False,
        include_paths: List[str] = None,
    ) -> None:
        """
        Compile the given Stan program file.  Translates the Stan code to
        C++, then calls the C++ compiler.

        :param opt_lvl: Optimization level used by the C++ compiler, one of
            {0, 1, 2, 3}.  Defaults to level 2. Level 0 optimization results
            in the shortest compilation time with code that may run slowly.
            Higher optimization levels increase runtime performance but will
            take longer to compile.

        :param overwrite: When True, existing executable will be overwritten.
            Defaults to False.

        :param include_paths: List of paths to directories where Stan should
            look for files to include in compilation of the C++ executable.
        """
        if self._exe_file is not None and not overwrite:
            print('model is already compiled')
            return
        hpp_file = os.path.splitext(self._stan_file)[0] + '.hpp'
        hpp_file = Path(hpp_file).as_posix()
        if overwrite or not os.path.exists(hpp_file):
            print('translating to {}'.format(hpp_file))
            stanc_path = os.path.join(
                cmdstan_path(), 'bin', 'stanc' + EXTENSION
            )
            stanc_path = Path(stanc_path).as_posix()
            cmd = [
                stanc_path,
                '--o={}'.format(hpp_file),
                Path(self._stan_file).as_posix(),
            ]
            if include_paths is not None:
                bad_paths = [d for d in include_paths if not os.path.exists(d)]
                if any(bad_paths):
                    raise Exception(
                        'invalid include paths: {}'.format(', '.join(bad_paths))
                    )
                cmd.append(
                    '--include_paths='
                    + ','.join((Path(p).as_posix() for p in include_paths))
                )
            print('stan to c++: make args {}'.format(cmd))
            do_command(cmd)
            if not os.path.exists(hpp_file):
                raise Exception('syntax error'.format(self._stan_file))

        exe_file, _ = os.path.splitext(os.path.abspath(self._stan_file))
        exe_file = Path(exe_file).as_posix()
        exe_file += EXTENSION
        make = os.getenv('MAKE', 'make')
        cmd = [make, 'O={}'.format(opt_lvl), exe_file]
        print('compiling c++: make args {}'.format(cmd))
        try:
            do_command(cmd, cmdstan_path())
        except Exception as e:
            print('make cmd failed\n', e)
        self._exe_file = exe_file
        print('compiled model file: {}'.format(self._exe_file))

    def sample(
        self,
        data: Union[Dict, str] = None,
        chains: int = 4,
        cores: int = 1,
        seed: Union[int, List[int]] = None,
        chain_ids: Union[int, List[int]] = None,
        inits: Union[Dict, float, str, List[str]] = None,
        warmup_iters: int = None,
        sampling_iters: int = None,
        save_warmup: bool = False,
        thin: int = None,
        max_treedepth: float = None,
        metric: Union[str, List[str]] = None,
        step_size: Union[float, List[float]] = None,
        adapt_engaged: bool = True,
        adapt_delta: float = None,
        csv_basename: str = None,
        show_progress: bool = False,
    ) -> StanFit:
        """
        Run or more chains of the NUTS sampler to produce a set of draws
        from the posterior distribution of a model conditioned on some data.

        This function validates the specified configuration, composes a call to
        the CmdStan ``sample`` method and spawns one subprocess per chain to run
        the sampler and waits for all chains to run to completion.
        Unspecified arguments are not included in the call to CmdStan, i.e.,
        those arguments will have CmdStan default values.

        For each chain, the ``StanFit`` object records the command,
        the return code, the sampler output file paths, and the corresponding
        subprocess console outputs, if any.

        :param data: Values for all data variables in the model, specified
            either as a dictionary with entries matching the data variables,
            or as the path of a data file in JSON or Rdump format.

        :param chains: Number of sampler chains, should be > 1.

        :param cores: Number of processes to run in parallel. Must be an
            integer between 1 and the number of CPUs in the system.

        :param seed: The seed for random number generator or a list of per-chain
            seeds. Must be an integer between 0 and 2^32 - 1. If unspecified,
            numpy.random.RandomState() is used to generate a seed which will be
            used for all chains. When the same seed is used across all chains,
            the chain-id is used to advance the RNG to avoid dependent samples.

        :param chain_ids: The offset for the random number generator, either
            an integer or a list of unique per-chain offsets.  If unspecified,
            chain ids are numbered sequentially starting from 1.

        :param inits: Specifies how the sampler initializes parameter values.
            Initializiation is either uniform random on a range centered on 0,
            exactly 0, or a dictionary or file of initial values for some or all
            parameters in the model.  The default initialization behavoir will
            initialize all parameter values on range [-2, 2] on the
            _unconstrained_ support.  If the expected parameter values are
            too far from this range, this option may improve adaptation.
            The following value types are allowed:

            * Single number ``n > 0`` - initialization range is [-n, n].
            * ``0`` - all parameters are initialized to 0.
            * dictionary - pairs parameter name : initial value.
            * string - pathname to a JSON or Rdump data file.
            * list of strings - per-chain pathname to data file.

        :param warmup_iters: Number of warmup iterations for each chain.

        :param sampling_iters: Number of draws from the posterior for each
            chain.

        :param save_warmup: When True, sampler saves warmup draws as part of
            the Stan csv output file.

        :param thin: Period between saved samples.

        :param max_treedepth: Maximum depth of trees evaluated by NUTS sampler
            per iteration.

        :param metric: Specification of the mass matrix, either as a
            vector consisting of the diagonal elements of the covariance
            matrix (``diag`` or ``diag_e``) or the full covariance matrix
            (``dense`` or ``dense_e``).

            If the value of the metric argument is a string other than
            ``diag``, ``diag_e``, ``dense``, or ``dense_e``, it must be
            a valid filepath to a JSON or Rdump file which contains an entry
            ``inv_metric`` whose value is either the diagonal vector or
            the full covariance matrix.

            If the value of the metric argument is a list of paths, its
            length must match the number of chains and all paths must be
            unique.

        :param step_size: Initial stepsize for HMC sampler.  The value is either
            a single number or a list of numbers which will be used as the
            global or per-chain initial step_size, respectively.
            The length of the list of step sizes must match the number of
            chains.

        :param adapt_engaged: When True, adapt stepsize and metric.
            *Note: If True, ``warmup_iters`` must be > 0.*

        :param adapt_delta: Adaptation target Metropolis acceptance rate.
            The default value is 0.8.  Increasing this value, which must be
            strictly less than 1, causes adaptation to use smaller step sizes.
            It improves the effective sample size, but may increase the time
            per iteration.

        :param csv_basename: A path or file name which will be used as the
            base name for the sampler output files.  The csv output files
            for each chain are written to file ``<basename>-<chain_id>.csv``
            and the console output and error messages are written to file
            ``<basename>-<chain_id>.txt``.
        """
        if chains < 1:
            raise ValueError(
                'chains must be a positive integer value, found {}'.format(
                    chains
                )
            )

        if chain_ids is None:
            chain_ids = [x + 1 for x in range(chains)]
        else:
            if type(chain_ids) is int:
                if chain_ids < 1:
                    raise ValueError(
                        'chain_id must be a positive integer value,'
                        ' found {}'.format(chain_ids)
                    )
                offset = chain_ids
                chain_ids = [x + offset + 1 for x in range(chains)]
            else:
                if not len(chain_ids) == chains:
                    raise ValueError(
                        'chain_ids must correspond to number of chains'
                        ' specified {} chains, found {} chain_ids'.format(
                            chains, len(chain_ids)
                        )
                    )
                for i in len(chain_ids):
                    if chain_ids[i] < 1:
                        raise ValueError(
                            'chain_id must be a positive integer value,'
                            ' found {}'.format(chain_ids[i])
                        )

        if cores < 1:
            raise ValueError(
                'cores must be a positive integer value, found {}'.format(cores)
            )
        if cores > cpu_count():
            print(
                'requested {} cores, only {} available'.format(
                    cores, cpu_count()
                )
            )
            cores = cpu_count()

        if data is not None:
            if isinstance(data, dict):
                with tempfile.NamedTemporaryFile(
                    mode='w+', suffix='.json', dir=TMPDIR, delete=False
                ) as fd:
                    data_file = fd.name
                    print('input data tempfile: {}'.format(fd.name))
                    jsondump(data_file, data)
                data = data_file

        if inits is not None:
            if isinstance(inits, dict):
                with tempfile.NamedTemporaryFile(
                    mode='w+', suffix='.json', dir=TMPDIR, delete=False
                ) as fd:
                    inits_file = fd.name
                    print('inits tempfile: {}'.format(fd.name))
                    jsondump(inits_file, inits)
                inits = inits_file
            # TODO:  issue 49: inits can be initialization function

        sampler_args = SamplerArgs(
            warmup_iters=warmup_iters,
            sampling_iters=sampling_iters,
            save_warmup=save_warmup,
            thin=thin,
            max_treedepth=max_treedepth,
            metric=metric,
            step_size=step_size,
            adapt_engaged=adapt_engaged,
            adapt_delta=adapt_delta,
        )

        args = CmdStanArgs(
            self._name,
            self._exe_file,
            chain_ids=chain_ids,
            data=data,
            seed=seed,
            inits=inits,
            output_basename=csv_basename,
            method_args=sampler_args,
        )

        stanfit = StanFit(args=args, chains=chains)
        try:
            tp = ThreadPool(cores)
            for i in range(chains):
                tp.apply_async(self._do_sample, (stanfit, i))
        finally:
            tp.close()
            tp.join()
        if not stanfit._check_retcodes():
            msg = 'Error during sampling'
            for i in range(chains):
                if stanfit._retcode(i) != 0:
                    msg = '{}, chain {} returned error code {}'.format(
                        msg, i, stanfit._retcode(i)
                    )
            raise Exception(msg)
        stanfit._validate_csv_files()
        return stanfit

    def _do_sample(self, stanfit: StanFit, idx: int) -> None:
        """
        Encapsulates call to sampler.
        Spawn process, capture console output to file, record returncode.
        """
        cmd = stanfit.cmds[idx]
        print('start chain {}.  '.format(idx + 1))
        proc = subprocess.Popen(
            cmd.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        proc.wait()
        stdout, stderr = proc.communicate()
        transcript_file = stanfit.console_files[idx]
        print('finish chain {}.  '.format(idx + 1))
        with open(transcript_file, 'w+') as transcript:
            if stdout:
                transcript.write(stdout.decode('utf-8'))
            if stderr:
                transcript.write('ERROR')
                transcript.write(stderr.decode('utf-8'))
        stanfit._set_retcode(idx, proc.returncode)