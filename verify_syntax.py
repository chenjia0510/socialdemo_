import ast, pathlib
for p in ['social_demotest/models.py','social_demotest/routers/system.py','social_demotest/routers/chat.py']:
    ast.parse(pathlib.Path(p).read_text(encoding='utf-8'))
print('syntax ok')
