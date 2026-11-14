"""Check installed pack readers and the actual model configuration without loading a GPU."""
import pathlib
import sys

tree, config, models = map(pathlib.Path, sys.argv[1:])
sys.path[:0] = [str(tree), str(tree / 'tools')]
import model_file
from serve.server import build_parser
from serve.config import model_settings, check_engine_flags, engine_flags_of

engine = str(tree / 'bin/basalt')
flags = engine_flags_of(engine)
if not flags:
    raise RuntimeError('cannot read candidate engine flags')
settings = model_settings(build_parser(), ['--engine-bin', engine, '--config', str(config)])
packs = set(models.glob('*.basalt'))
for args, argv, file_words, cli_words in settings:
    check_engine_flags(file_words, flags, 'model configuration')
    check_engine_flags(cli_words, flags, 'command line')
    if args.skipped:
        raise RuntimeError('candidate dropped configured settings: ' + ', '.join(args.skipped))
    packs.add(pathlib.Path(args.model))
for pack in sorted(packs):
    model_file.ModelFile(str(pack))
    print('pack readable:', pack.name)
print('configuration and installed packs accepted')
