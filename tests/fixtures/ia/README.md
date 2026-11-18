# Respuestas de IA para pruebas

Sobres con la forma documentada de `claude -p --output-format json`
(`type`, `subtype`, `is_error`, `result`, `usage`, `total_cost_usd`). Son
sintéticos: ninguno se obtuvo de un backend real, porque enviar páginas de los
PDFs a un servicio externo requiere autorización explícita. El harness falso de
`tests/test_agente_verificador.py` los imprime como si fuera `claude -p`.
