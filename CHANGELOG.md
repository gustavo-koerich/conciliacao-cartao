# Histórico de alterações

Cada versão corresponde a um commit (o número aparece no topo da aplicação, ao lado do IP). Detalhes: `git show <commit>`.

## 17/11/2026 — valor "1.180.00" no formulário + novo visual
- Formulário com valor escrito "1.180.00" ou "1,180.00" (ponto nos centavos) agora é lido como R$ 1.180,00. Antes ficava "valor ilegível", não casava com a fatura e a compra aparecia duas vezes (Ana, MG ELETRONICOS 15/10). Vale para planilhas enviadas depois da publicação; resposta já gravada se resolve com "corrigir valor" ou reenviando a planilha.
- Visual no padrão da Central (igual ao Dashboard Diretoria e à Frota Comercial): logo, cores da marca, fontes Barlow, menu em abas, botão de tema único (claro padrão; a escolha vale para a Central inteira). Portal do representante com as mesmas cores.

## 16/10/2026 — preparação para o mini servidor
- Erro na leitura do resumo da fatura: mostra o que deu para ler, pergunta se é relevante; não → conferir/corrigir os dados e seguir; sim → relatório para o suporte com o texto lido.
- Central: acessos por módulo (Central → Usuários → Acessos); quem tem só a Conciliação entra direto nela; "Sair" na Conciliação e link "Central" para quem tem outros módulos.
- Fatura que não fecha: pergunta se a diferença é relevante; dá para **ignorar e seguir** (com motivo registrado) ou gerar o **relatório para o suporte**. Versão nova de uma fatura já em uso entra só com "Usar esta versão".
- Notas/controle: compra que bate com o controle mas sem nota = **conciliado sem nota** (lista própria na tela de notas).
- Exportação da base (CSV por tabela + dicionário de dados) e comando para limpar a base.
- Versão do sistema (commit) no topo da tela e no relatório do suporte.

## 16/10/2026
- 374aff4 — O valor manda: casamento só pelo valor concilia com ressalva; botão "Desfazer" na nota casada.
- 4697cf1 — Planilha de controle de compras nos cartões de notas fiscais.
- a8e3bcc — Fatura Ailos de outubro: corrige a leitura do período (REF … A 1 OUT).
- 2e4a740 — Corrige o histórico de faturas quando uma leitura falhou sem competência.
- f07d47b — Notas em lote: limite de arquivos, XML no lugar do PDF.
- a28ac1c — Notas fiscais em lote e cadastro manual de cartão por banco.
- 24a37a7 — Cadastros pela tela, seletor banco/ano/mês e cartões de notas fiscais.
- 3f0058e — Vários bancos: cadastro de contas de cartão e leitor do Banco do Brasil.
- c9bdd1c — Análise de gastos: dashboard interativo e lista geral.

## 07/10/2026
- 7698606 — Ajustes manuais, comprovante pela tela, valor declarado e planilha por período.

## 06/10/2026
- f3f5b34 — Importação da planilha, motor de conciliação e telas.
- 1539939 — Leitura da fatura (texto e OCR) com trava de totais.
- 3bb69be — Models, migrations, parâmetros e carga de cadastro.
- 9745bca — Estrutura, Docker, acesso pela Central e filtro de rede.
