"""Serviço do agente do Canteiro.

O agente conversa, consulta a API central com o token de quem pergunta e, para escrever, só cria propostas
(`POST /propostas`). Conta nenhuma é feita aqui nem no modelo: os números vêm prontos da API (`regras.conta_de_falta`).
"""
