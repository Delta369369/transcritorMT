# Transcritor — by Delta_Victor

Menu com identidade visual inspirada na arte neon: fundo preto, moldura verde e assinatura **by Delta_Victor**. A abertura usa uma adaptação em texto para PowerShell 7, sem dependências gráficas.

## Estrutura

```text
Transcritor/
├── Abrir_Transcritor_Admin.bat
└── Apoio/
    ├── Transcrição_Única.py
    ├── Transcrição_lote.py
    ├── Testar_máquina.py
    ├── requirements.txt
    └── README.md
```

Não inclui ambiente Python, bibliotecas, modelos, caches, testes nem exemplos. Os formatos TXT e DOCX estão definidos no código.

## Requisitos iniciais

Windows com PowerShell 7 e um Python instalado para iniciar o auxiliar. O ambiente do transcritor usa exclusivamente Python 3.12 de 64 bits: o auxiliar reaproveita uma instalação compatível ou instala a versão recomendada automaticamente, preservando o Python 3.15.2 e as demais instalações. Para a preparação inicial, é necessário internet e espaço em disco para bibliotecas e modelo. O arquivo ZIP é pequeno; os componentes baixados depois podem ocupar vários GB.

## Primeiro uso

1. Extraia o ZIP inteiro para uma pasta com permissão de escrita. Não execute o BAT de dentro do ZIP.
2. Abra Abrir_Transcritor_Admin.bat, confirme o UAC e escolha 1-Testar a Máquina.

O menu abre diretamente o executável do PowerShell 7 como administrador. A opção 1 inicia pelo Python do sistema, localiza/prepara o Python 3.12 de 64 bits e usa esse executável para criar Apoio/.venv; depois lê requirements.txt, instala somente dependências ausentes/incompatíveis e testa áudio, Word e inferência CPU com large-v3-turbo. O modelo é baixado se ainda não estiver disponível no cache compatível do Hugging Face.

FFmpeg/FFprobe do sistema são reaproveitados quando funcionam; caso contrário, o auxiliar prepara as versões portáteis. Se detectar NVIDIA no Windows, verifica cuBLAS/cuDNN, instala as bibliotecas necessárias localmente quando faltarem e testa a inferência GPU. Drivers de vídeo não são instalados automaticamente. GPU é opcional: uma falha nela é informada separadamente, mantendo a opção CPU quando esta passa nos testes.

Maquina OK! confirma o caminho CPU completo. Consulte também a mensagem específica de GPU: validada ou pendente. Se a instalação ou o teste CPU falhar, o auxiliar informa a pendência e não declara sucesso.

## Usar depois da preparação

No menu, escolha 2-Transcrição_Única.py e informe o caminho do arquivo ou pasta das mídias. A opção 3 executa Transcrição_lote.py para gerar relatórios consolidados. Enter retorna ao menu após a execução; 0-Sair fecha a janela.

A .venv e as bibliotecas válidas são reutilizadas nas próximas execuções. O modelo fica no cache do usuário, normalmente em %USERPROFILE%/.cache/huggingface/hub, e não na .venv. Um Whisper de outro programa/formato pode não ser compatível. Para um diretório CTranslate2 já disponível em outro local, use --modelo ao executar o script manualmente.

TXT e Word são salvos ao lado de cada mídia, com o prefixo Transcrição_. Novas execuções substituem relatórios de mesmo nome. Arquivos sem áudio ou sem fala são informados na tela, sem gerar relatórios vazios. As mídias originais são preservadas.

## Dependências

requirements.txt é a única lista de bibliotecas e versões. Os comentários delimitam três grupos lidos pelo auxiliar:

- base: reconhecimento de fala, Word e decodificação de áudio;
- media: FFmpeg/FFprobe portáteis, apenas quando necessários;
- gpu-windows: bibliotecas NVIDIA, apenas quando necessárias no Windows.

O auxiliar valida versões e imports antes de instalar. O pip também usa esse mesmo arquivo como restrições de compatibilidade. Para evitar baixar componentes GPU desnecessariamente, prefira a opção 1 ao comando manual pip install -r requirements.txt, que instala todos os requisitos aplicáveis ao sistema.

## Comandos opcionais

Na pasta Apoio, com Python do sistema:

```powershell
python ".\Testar_máquina.py"
python ".\Testar_máquina.py" --cpu
python ".\Testar_máquina.py" --sem-modelo --cpu
python ".\Transcrição_Única.py"
```

--cpu dispensa instalação/teste GPU. --sem-modelo faz apenas uma verificação parcial e não declara Maquina OK!. --dispositivo cpu no transcritor força CPU. --modelo aceita outro modelo ou diretório explícito. --hash verifica SHA-256 da mídia. --ffmpeg e --ffprobe aceitam executáveis explícitos. Consulte --help.

## Reconhecimento e relatórios

O padrão é large-v3-turbo em português, sem classificação de interlocutores e sem saída JSON. Usa GPU quando possível e, em falhas recuperáveis, tenta CPU/INT8 com o mesmo modelo. DLL CUDA ausente leva diretamente à CPU; falta de memória ainda permite alternativas CUDA. O vocabulário policial está embutido. O texto anterior não é usado como contexto entre janelas, para reduzir ciclos de repetição. Não há substituições automáticas nem exclusão automática de falas suspeitas. Confira nomes, números e passagens duvidosas no áudio.

TXT: separadores de 85 caracteres, títulos centralizados em 80 posições e corpo justificado com espaços entre palavras, sem espaços finais. A última linha de cada trecho fica à esquerda. Palavras não são cortadas; uma palavra isolada maior que a largura é preservada. Use fonte monoespaçada. Tempos no formato [HH:MM:SS].

DOCX: A4 retrato, margens de aproximadamente 1 cm, Palatino Linotype 12 pt, justificado, entrelinhas simples, Português (Brasil). Destaques por formatação direta, 14 pt, negrito e sublinhado; título principal e encerramento centralizados. Todos os parágrafos usam Normal. Linhas em branco separam os blocos e os trechos. Cabeçalho/rodapé vazios. Não depende de Normal.dotm nem de macros.

## Outra máquina

Leve apenas este pacote mínimo e execute a opção 1. Não copie .venv nem caches da máquina anterior. Se uma .venv existente estiver inválida, o auxiliar a preserva como .venv_anterior_<identificador> e prepara outra. O ambiente é instalado dentro de Apoio; o cache do modelo é compartilhado pelo usuário fora do pacote.

## Python do ambiente

Python 3.15 pode iniciar o auxiliar, mas não é usado na .venv do transcritor por incompatibilidade atual do ONNX Runtime. O auxiliar verifica exatamente Python 3.12 de 64 bits antes de instalar dependências. Se necessário, usa o Python Install Manager oficial para obter a série 3.12; como alternativa, baixa o instalador Windows oficial 3.12.10 de python.org, valida a assinatura da Python Software Foundation e instala para o usuário, sem adicionar ao PATH nem substituir o lançador. Uma .venv com outra versão é preservada e recriada automaticamente. A opção 2 orienta executar a opção 1 se detectar uma versão incompatível.

## Transcrição única e lote

- Opção 2 — Transcrição_Única.py [ideal para oitivas]: o arquivo anterior apenas recebeu outro nome. Motor, vocabulário policial, reconhecimento e modelos TXT/DOCX foram preservados, inclusive os tempos. O comportamento existente de aceitar uma pasta e gerar relatórios individuais por mídia também foi mantido.
- Opção 3 — Transcrição_Lote.py [ideal para audios diversos]: executa o arquivo Transcrição_lote.py e percorre uma pasta e suas subpastas, em ordem, reutilizando o modelo. Gera um único TXT e um único DOCX consolidados por execução. Não aplica o vocabulário policial e não imprime tempos nos textos do lote.

Cada seção do lote contém ARQUIVO_01/NN, nome/formato, tipo de mídia, data, duração e transcrição, conforme o modelo fornecido. Caminhos relativos diferenciam nomes repetidos em subpastas. Arquivos com falha, sem áudio ou sem fala são identificados com avisos; os demais continuam sendo processados. O TXT é salvo após cada arquivo. O DOCX consolidado é salvo ao terminar.

Os relatórios do lote recebem nomes Transcrição_Lote_AAAAMMDD_HHMMSS.txt e .docx na pasta de origem. A opção --saida permite escolher outra pasta. Cada execução usa nomes livres, preservando relatórios anteriores. Os dois modos compartilham a mesma .venv, dependências e modelo baixado. Não há dependências novas.

```powershell
python ".\Transcrição_Única.py"
python ".\Transcrição_lote.py" "C:\pasta\das\midias"
python ".\Transcrição_lote.py" "C:\pasta\das\midias" --saida "C:\pasta\dos\relatorios"
```
