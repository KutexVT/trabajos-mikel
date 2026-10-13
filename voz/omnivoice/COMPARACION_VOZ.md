# Comparación de voz de Mikel

La nueva referencia quedó seleccionada por Mikel después de escuchar ambas muestras.
Se sustituyeron únicamente el audio de referencia y su transcripción. Se mantienen
16 pasos, 4 hilos, velocidad 0.9, probabilidad 15%, pausa de 30 minutos y límite de 90 segundos.

## Prueba controlada

Se usó la misma frase y semilla para comparar cuatro variantes. La similitud es el
coseno medio de los vectores de voz frente a 37 audios propios distintos de las referencias.
Es un indicador relativo, no un porcentaje de parecido ni una garantía de identidad.

| Variante | Similitud media | Generación | Duración del audio |
|---|---:|---:|---:|
| Referencia anterior · 16 pasos | 0.368 | 43.9 s | 3.08 s |
| Referencia anterior · 32 pasos | 0.341 | 87.7 s | 3.25 s |
| Nueva referencia · 16 pasos | 0.488 | 43.5 s | 3.05 s |
| Otra referencia más larga · 16 pasos | 0.385 | 66.9 s | 4.08 s |

Los tiempos de esta tabla corresponden a síntesis con el modelo ya cargado.
La respuesta del bot también incluye carga inicial, reconocimiento y conversión.

## Segunda frase

Se cambió la frase y la semilla para comprobar que la ventaja no dependiera de una sola muestra.

| Referencia | Similitud media | Generación |
|---|---:|---:|
| Anterior | 0.312 | 45.5 s |
| Nueva | 0.485 | 44.9 s |

El reconocimiento local confirmó el contenido de las muestras seleccionadas.
La comparación automática utiliza [WeSpeaker ResNet34-LM](https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM),
entrenado en VoxCeleb; es una comprobación complementaria a la escucha de Mikel.
El modelo de evaluación y sus dependencias se usan solo en las pruebas locales.
No se incorporan al bot ni añaden tiempo a cada respuesta.

Se revisaron los [parámetros oficiales de OmniVoice](https://github.com/k2-fsa/OmniVoice/blob/master/docs/generation-parameters.md).
En esta comparación duplicar los pasos tardó aproximadamente el doble y no mejoró
la similitud con la voz habitual. La mejora elegida procede de la referencia.

## Archivos locales

- `muestra-voz-mikel.ogg`: muestra anterior
- `muestra-voz-mikel-mejorada.ogg`: muestra nueva elegida por Mikel
- `OMNIVOICE.md`: controles de uso

El perfil anterior queda guardado en `media/voz/config-anterior.json`. MUDO permanece
intacto. No se enviaron mensajes de prueba a WhatsApp.

## Verificación con el perfil seleccionado

El recorrido completo con el perfil nuevo tardó 53.99 segundos.
Se generó y transcribió audio real, se conservó la cita y se envió a un receptor HTTP
local. El segundo mensaje salió como texto por la pausa. No se contactó WhatsApp.
La segunda frase también coincidió con el texto esperado. MUDO y todos los cursores
conservaron sus hashes anteriores. `git diff --check` terminó sin errores.
