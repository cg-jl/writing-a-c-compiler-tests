"""Register allocation tests"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, List, Mapping, NamedTuple, Optional, Union

from . import basic
from .parser import asm, parse
from .parser.asm import Opcode, Register
from .tacky import common

CHAPTER = 20
TEST_DIR = basic.TEST_DIR.joinpath(f"chapter_{CHAPTER}").resolve()
# The wrapper script includes a handwritten assembly main function
# which validates that callee-saved registers are preserved
WRAPPER_SCRIPT: Path
if basic.IS_OSX:
    WRAPPER_SCRIPT = TEST_DIR.joinpath("wrapper_osx.s")
else:
    WRAPPER_SCRIPT = TEST_DIR.joinpath("wrapper_linux.s")


# TypeGuard would be better return value here, but 3.8 and 3.9 don't support it
# and we're avoiding additional dependencies like typing_extensions
def uses_stack(i: asm.AsmItem) -> bool:
    """Is this an instruction that accesses a value on the stack?"""
    if isinstance(i, asm.Label):
        return False

    def is_stack(operand: asm.Operand) -> bool:
        return isinstance(operand, asm.Memory) and operand.base == Register.BP

    return any(is_stack(op) for op in i.operands)


class TestRegAlloc(basic.TestChapter):
    """Test class for register allocation.

    We'll generate a test method for each C program in the chapter_20/ directory.
    Each dynamically generated test calls one of the main test methods defined below:

    * basic_test: make sure the program behaves correctly but don't inspect the assembly code
    * no_spills_test: make sure we can allocate every register without spilling
    * spill_test: the number of spilled pesudos and the number of instructions that
        access the stack should both be below some upper bound
    """

    @property
    def lib_path(self) -> Path:
        """Directory containing extra library code"""
        return self.test_dir.joinpath("libraries")

    def tearDown(self) -> None:
        """Delete files produced during this test run (e.g. assembly and object files)

        Don't delete the wrapper scripts!"""
        garbage_files = (
            f
            for f in self.test_dir.rglob("*")
            if not f.is_dir()
            and f.suffix not in [".c", ".h"]
            and f.stem not in ["wrapper_osx", "wrapper_linux"]
        )

        for f in garbage_files:
            f.unlink()

    def basic_test(self, program_path: Path, extra_lib: Optional[Path] = None) -> None:
        """Test that the compiled program behaves correctly but don't inspect the assembly code.

        Compile the program, linking against the wrapper script (which defines main) and any extra
        libraries, then run it and validate the result.

        Tests that _do_ inspect the assembly code should first call basic_test to make sure
        the program behaves correctly, then parse the assembly file and perform further validation.

        Args:
            program_path: Absoulte path to C or assembly file to compile and run
            extra_lib: An extra library we need to compile with
        """

        if program_path.suffix == ".s":
            # caller already compiled it to assembly
            input_files = [program_path, WRAPPER_SCRIPT]
        else:
            compilation_result = self.invoke_compiler(program_path, cc_opt="-c")
            self.assertEqual(
                compilation_result.returncode,
                0,
                msg=f"compilation of {program_path} failed with error:\n\
                    {compilation_result.stderr}",
            )
            input_files = [program_path.with_suffix(".o"), WRAPPER_SCRIPT]

        if extra_lib:
            input_files.append(self.lib_path / extra_lib)

        actual_result = basic.gcc_compile_and_run(input_files, [])
        # make sure behavior is the same
        # NOTE: if program_path is assembly file (because we were called from another test method
        # that's going to parse this file and perform more validation on it),
        # we should ook up corresponding C file in EXPECTED_RESULTS
        key = program_path.with_suffix(".c")
        self.validate_runs(key, actual_result)

    def run_and_parse(
        self,
        program_path: Path,
        extra_lib: Optional[Path] = None,
        target_fun: str = "target",
    ) -> asm.AssemblyFunction:
        """Shared logic for register allocation tests that validate assembly code.
        1. Compile the file at program_path to assembly
        2. Call basic_test to make sure it behaves correctly
        3. Parse assembly file and return it
        The caller can then perform further validation on the parsed assembly code

        Args:
            program_path: Absolute path to C file under test
            extra_lib: Additional library files to link against
            target_fun: Name of function to parse/inspect
        Returns:
            Parsed assembly code for specified target fun
        """

        # first compile to assembly
        try:
            self.invoke_compiler(program_path, cc_opt="-s").check_returncode()
        except subprocess.CalledProcessError as e:
            self.fail(f"Compilation failed:\n{e.stderr}")
        asm_file = program_path.with_suffix(".s")

        # make sure behavior is the same
        self.basic_test(asm_file, extra_lib=extra_lib)

        # make sure we actually performed the optimization
        parsed_asm = parse.parse_file(asm_file)[target_fun]

        return parsed_asm

    def no_spills_test(
        self,
        program_path: Path,
        extra_lib: Optional[Path] = None,
        target_fun: str = "target",
    ) -> None:
        """Test that we allocated every register in target_fun without spilling.
        First make sure behavior is correct, then examine parsed assembly
        to validate that we never access the stack

        Args:
            program_path: Absolute path to C file under test
            extra_lib: Additional library files to link against
            target_fun: Name of function to parse/inspect
        """

        # validate behavior + get parsed assembly
        parsed_asm = self.run_and_parse(
            program_path=program_path,
            extra_lib=extra_lib,
            target_fun=target_fun,
        )

        # make sure no instructions use stack
        bad_instructions = [i for i in parsed_asm.instructions if uses_stack(i)]
        self.assertFalse(
            bad_instructions,
            msg=common.build_msg(
                "Found instructions that use operands on the stack",
                bad_instructions=bad_instructions,
                full_prog=parsed_asm,
                program_path=program_path,
            ),
        )

    def spill_test(
        self,
        program_path: Path,
        max_spilled_instructions: int,
        max_spilled_pseudos: int,
        extra_lib: Optional[Path] = None,
        target_fun: str = "target",
    ) -> None:
        """Test for a program with so many conflicts that it spills.
        First validate the compiled program's behavior, then make sure we don't
        have more than the expected number of distinct spilled pseudoregisters,
        or produce more than the expected number of instructions that access the stack

        Args:
            program_path: Absolute path to C file under test
            max_spilled_instructions: maximum number of instructions that access the stack
            max_spilled_pseudos: maximum number of distinct stack addresses accessed
            extra_lib: Additional library files to link against
            target_fun: Name of function to parse/inspect
        """

        parsed_asm = self.run_and_parse(
            program_path=program_path,
            target_fun=target_fun,
            extra_lib=extra_lib,
        )

        spill_instructions = [
            i
            for i in parsed_asm.instructions
            if uses_stack(i) and i.opcode == Opcode.MOV  # type: ignore # use_stack guarantees this is an instruction
        ]
        self.assertLessEqual(
            len(spill_instructions),
            max_spilled_instructions,
            msg=common.build_msg(
                f"Should only need {max_spilled_instructions} instructions \
                    involving spilled pseudo but found {len(spill_instructions)}",
                bad_instructions=spill_instructions,
                full_prog=parsed_asm,
                program_path=program_path,
            ),
        )

        spilled_operands = set(
            [
                str(op)  # convert to string b/c Operands themselves are not hashable
                for i in spill_instructions
                for op in i.operands  # type: ignore
                if isinstance(op, asm.Memory)
            ]
        )
        self.assertLessEqual(
            len(spilled_operands),
            max_spilled_pseudos,
            msg=common.build_msg(
                f"At most {max_spilled_pseudos} pseudoregs should have been spilled, \
                    looks like {len(spilled_operands)} were",
                bad_instructions=spill_instructions,
                full_prog=parsed_asm,
                program_path=program_path,
            ),
        )

    def coalescing_test(
        self,
        program_path: Path,
        extra_lib: Optional[Path] = None,
        target_fun: str = "target",
        max_moves: int = 0,
    ) -> None:
        """Test that we perform register coalescing properly.

        First validate the compiled program's behavior, then make sure we don't
        have more than the expected number of mov instructions where the source
        and destination are both registers.

        Args:
            program_path: Absolute path to C file under test
            extra_lib: Additional library files to link against
            target_fun: Name of function to parse/inspect
            max_moves: maximum number of mov instructions between registers
        """

        def is_mov_between_regs(i: asm.AsmItem) -> bool:
            """Check whether this is a move between registers (other than RBP/RSP)"""
            if common.is_mov(i):
                src, dst = i.operands[0], i.operands[1]  # type: ignore  # is_mov guarantees it's an instruction
                return (
                    isinstance(src, asm.Register)
                    and src not in [Register.BP, Register.SP]
                    and isinstance(dst, asm.Register)
                    and dst not in [Register.BP, Register.SP]
                )

            # otherwise, not a mov
            return False

        parsed_asm = self.run_and_parse(
            program_path,
            extra_lib=extra_lib,
            target_fun=target_fun,
        )

        bad_instructions = [i for i in parsed_asm.instructions if uses_stack(i)]
        mov_instructions = [
            i for i in parsed_asm.instructions if is_mov_between_regs(i)
        ]
        self.assertFalse(
            bad_instructions,
            msg=common.build_msg(
                "Found instructions that use operands on the stack",
                bad_instructions=bad_instructions,
                full_prog=parsed_asm,
                program_path=program_path,
            ),
        )
        self.assertLessEqual(
            len(mov_instructions),
            max_moves,
            msg=common.build_msg(
                f"Expected at most {max_moves} move instructions but found {len(mov_instructions)}",
                bad_instructions=mov_instructions,
                full_prog=parsed_asm,
                program_path=program_path,
            ),
        )


# define what kind of validation to perform for each C program
class NoSpillTest(NamedTuple):
    extra_lib: Optional[Path] = None
    target_fun: str = "target"


class SpillTest(NamedTuple):
    max_spilled_pseudos: int
    max_spilled_instructions: int
    extra_lib: Optional[Path] = None
    target_fun: str = "target"


class CoalesceTest(NamedTuple):
    extra_lib: Optional[Path] = None
    target_fun: str = "target"
    max_moves: int = 0


REGALLOC_TESTS: Mapping[str, Union[CoalesceTest, NoSpillTest, SpillTest]] = {
    "trivially_colorable.c": NoSpillTest(),
    "use_all_hardregs.c": NoSpillTest(),
    "spill_callee_saved.c": NoSpillTest(),
    "preserve_across_fun_call.c": NoSpillTest(),
    "track_arg_registers.c": NoSpillTest(extra_lib=Path("track_arg_registers_lib.c")),
    "many_pseudos_fewer_conflicts.c": NoSpillTest(
        extra_lib=Path("many_pseudos_fewer_conflicts_lib.c"),
        target_fun="no_spills",
    ),
    "cmp_liveness.c": NoSpillTest(),
    "copy_no_interference.c": NoSpillTest(),
    "same_instr_no_interference.c": NoSpillTest(),
    "loop.c": NoSpillTest(),
    "dbl_trivially_colorable.c": NoSpillTest(),
    "fourteen_pseudos_interfere.c": NoSpillTest(),
    "push_xmm.c": NoSpillTest(),
    "track_dbl_arg_registers.c": NoSpillTest(
        extra_lib=Path("track_dbl_arg_registers_lib.c")
    ),
    "store_pointer_in_register.c": NoSpillTest(),
    "callee_saved_live_at_exit.c": NoSpillTest(
        extra_lib=Path("callee_saved_live_at_exit_lib.c"),
        target_fun="cant_coalesce_fully",
    ),
    "funcall_generates_args.c": NoSpillTest(
        extra_lib=Path("funcall_generates_args_lib.c")
    ),
    "force_spill.c": SpillTest(
        max_spilled_instructions=3,
        max_spilled_pseudos=1,
        extra_lib=Path("force_spill_lib.c"),
    ),
    # possibly these rewrite instructions don't belong in reg allocation test suite
    "spills_and_rewrites.c": SpillTest(
        max_spilled_instructions=10,
        max_spilled_pseudos=3,
        extra_lib=Path("force_spill_lib.c"),
    ),
    "spills_rewrites_compare.c": SpillTest(
        max_spilled_instructions=3,
        max_spilled_pseudos=1,
        extra_lib=Path("force_spill_lib.c"),
    ),
    "rewrite_large_multiply.c": SpillTest(
        max_spilled_instructions=4,
        max_spilled_pseudos=1,
        extra_lib=Path("force_spill_lib.c"),
    ),
    "spill_movz_dst.c": SpillTest(
        max_spilled_instructions=6,
        max_spilled_pseudos=3,
        extra_lib=Path("force_spill_lib.c"),
    ),
    "test_spill_metric.c": SpillTest(
        max_spilled_instructions=3,
        max_spilled_pseudos=1,
        extra_lib=Path("test_spill_metric_lib.c"),
    ),
    "test_spill_metric_2.c": SpillTest(
        max_spilled_instructions=3,
        max_spilled_pseudos=1,
        extra_lib=Path("test_spill_metric_2_lib.c"),
    ),
    "copy_and_separate_interference.c": SpillTest(
        max_spilled_pseudos=1, max_spilled_instructions=3
    ),
    "optimistic_coloring.c": SpillTest(
        max_spilled_pseudos=5,
        max_spilled_instructions=20,
        target_fun="five_spills",
    ),
    "test_spilling_dbls.c": SpillTest(
        max_spilled_instructions=4,
        max_spilled_pseudos=1,
        extra_lib=Path("force_spill_dbl_lib.c"),
    ),
    "mixed_ints.c": SpillTest(
        max_spilled_instructions=2,
        max_spilled_pseudos=1,
        extra_lib=Path("force_spill_mixed_int_lib.c"),
    ),
    "briggs_coalesce.c": CoalesceTest(),
    "briggs_coalesce_tmps.c": CoalesceTest(target_fun="briggs"),
    "george_coalesce.c": CoalesceTest(extra_lib=Path("george_lib.c")),
    "coalesce_prevents_spill.c": CoalesceTest(
        extra_lib=Path("coalesce_prevents_spill_lib.c"), max_moves=11
    ),
}


def make_regalloc_test(
    program: Path, no_coalescing: bool
) -> Callable[[TestRegAlloc], None]:
    """Generate test method for a single test program."""

    # Look up what kind of test to run and any extra arguments for that test
    test_info = REGALLOC_TESTS.get(program.name)

    if test_info is None:
        # default test: make sure the program behaves correctly but don't validate assembly
        return basic.make_test_run(program)

    if "with_coalescing" in program.parts and no_coalescing:
        # if this is a coalescing test but we haven't implemented coalescing yet,
        # make sure it runs correctly but don't validate assembly
        extra_lib = test_info.extra_lib

        def test(self: TestRegAlloc) -> None:
            self.basic_test(program, extra_lib=extra_lib)

    elif isinstance(test_info, NoSpillTest):
        # assign test_info to another variable to make mypy happy
        # see https://github.com/python/mypy/issues/2608
        nospilltest_info = test_info

        def test(self: TestRegAlloc) -> None:
            self.no_spills_test(
                program,
                extra_lib=nospilltest_info.extra_lib,
                target_fun=nospilltest_info.target_fun,
            )

    elif isinstance(test_info, SpillTest):
        spilltest_info = test_info

        def test(self: TestRegAlloc) -> None:
            self.spill_test(
                program,
                spilltest_info.max_spilled_instructions,
                spilltest_info.max_spilled_pseudos,
                spilltest_info.extra_lib,
            )

    else:
        ti: CoalesceTest = test_info

        def test(self: TestRegAlloc) -> None:
            self.coalescing_test(
                program,
                target_fun=ti.target_fun,
                extra_lib=ti.extra_lib,
                max_moves=ti.max_moves,
            )

    return test


def configure_tests(
    compiler: Path,
    options: List[str],
    extra_credit_flags: basic.ExtraCredit,
    int_only: bool,
    no_coalescing: bool,
) -> None:
    """Dynamically add test methods and attributes to TestRegAlloc.

    Args:
        compiler: absolute path to compiler under test
        options: extra command-line arguments to pass through to compiler
        extra_credit_flags: extra credit features to test, represented as a bit vector
        int_only: the reader skipped Part II;
            only include tests that rely on Part I language features
        no_coalescing: the reader hasn't implemented register coalescing yet, so don't test for it
    """

    # set class attributes
    setattr(TestRegAlloc, "test_dir", TEST_DIR)
    setattr(TestRegAlloc, "cc", compiler)
    setattr(TestRegAlloc, "options", options)
    # can't test intermediate stages for reg allocation
    setattr(TestRegAlloc, "exit_stage", None)

    # include all test programs in chapter_20/int_only/
    # if the reader completed part II, also include all the test programs in chapter_20/all_types/
    if int_only:
        subdirs = ["int_only"]
    else:
        subdirs = ["int_only", "all_types"]

    all_tests = [p for subdir in subdirs for p in (TEST_DIR / subdir).rglob("*.c")]

    for program in all_tests:
        if basic.excluded_extra_credit(program, extra_credit_flags):
            continue
        key = program.relative_to(TEST_DIR).with_suffix("")
        name = f"test_{key}"
        setattr(TestRegAlloc, name, make_regalloc_test(program, no_coalescing))
