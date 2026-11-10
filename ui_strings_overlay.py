"""Reviewed interface translations for the overlay and its channel drawer.

Keys are English UI sources; chat contents, player names, channel protocol keys,
and model identifiers never pass through this catalog.
"""

LANGUAGES = ("es", "pt-BR", "ru", "pl", "de", "fr", "tr", "ko", "ja", "zh-CN")

_ROWS = {
    "Lineage 2 Chat Translator — Overlay": (
        "Lineage 2 Chat Translator — Superposición", "Lineage 2 Chat Translator — Sobreposição",
        "Lineage 2 Chat Translator — Оверлей", "Lineage 2 Chat Translator — Nakładka",
        "Lineage 2 Chat Translator — Overlay", "Lineage 2 Chat Translator — Superposition",
        "Lineage 2 Chat Translator — Katman", "Lineage 2 Chat Translator — 오버레이",
        "Lineage 2 Chat Translator — オーバーレイ", "Lineage 2 Chat Translator — 悬浮窗"),
    "Translations": (
        "Traducciones", "Traduções", "Переводы", "Tłumaczenia", "Übersetzungen", "Traductions",
        "Çeviriler", "번역", "翻訳", "译文"),
    "Settings": (
        "Ajustes", "Configurações", "Настройки", "Ustawienia", "Einstellungen", "Paramètres",
        "Ayarlar", "설정", "設定", "设置"),
    "Channels": (
        "Canales", "Canais", "Каналы", "Kanały", "Kanäle", "Canaux", "Kanallar", "채널", "チャネル", "频道"),
    "Lock": (
        "Bloquear", "Bloquear", "Закрепить", "Zablokuj", "Sperren", "Verrouiller", "Kilitle", "잠금", "固定", "锁定"),
    "Locked": (
        "Bloqueado", "Bloqueado", "Закреплён", "Zablokowano", "Gesperrt", "Verrouillé", "Kilitli", "잠김", "固定中", "已锁定"),
    "no channels": (
        "sin canales", "nenhum canal", "нет каналов", "brak kanałów", "keine Kanäle", "aucun canal",
        "kanal yok", "채널 없음", "チャネルなし", "未选频道"),
    "filtered": (
        "filtrado", "filtrado", "фильтр", "filtrowanie", "gefiltert", "filtré", "filtreli", "필터 적용", "絞り込み中", "已筛选"),
    "locked": (
        "bloqueado", "bloqueado", "закреплён", "zablokowano", "gesperrt", "verrouillé", "kilitli", "잠김", "固定中", "已锁定"),
    "Translation unavailable: ": (
        "Traducción no disponible: ", "Tradução indisponível: ", "Перевод недоступен: ", "Tłumaczenie niedostępne: ",
        "Übersetzung nicht verfügbar: ", "Traduction indisponible : ", "Çeviri kullanılamıyor: ",
        "번역을 사용할 수 없음: ", "翻訳できません: ", "翻译不可用："),
    "Sample": (
        "Ejemplo", "Exemplo", "Пример", "Przykład", "Beispiel", "Exemple", "Örnek", "예시", "サンプル", "示例"),
    "Notice": (
        "Aviso", "Aviso", "Уведомление", "Powiadomienie", "Hinweis", "Avis", "Bildirim", "알림", "通知", "通知"),
    "Overlay setting": (
        "Ajuste de superposición", "Configuração da sobreposição", "Настройка оверлея", "Ustawienie nakładki",
        "Overlay-Einstellung", "Paramètre de superposition", "Katman ayarı", "오버레이 설정", "オーバーレイ設定", "悬浮窗设置"),
    "Could not apply this setting: {error}": (
        "No se pudo aplicar este ajuste: {error}", "Não foi possível aplicar esta configuração: {error}",
        "Не удалось применить настройку: {error}", "Nie udało się zastosować ustawienia: {error}",
        "Diese Einstellung konnte nicht angewendet werden: {error}", "Impossible d’appliquer ce paramètre : {error}",
        "Bu ayar uygulanamadı: {error}", "이 설정을 적용할 수 없습니다: {error}", "この設定を適用できません: {error}", "无法应用此设置：{error}"),
    "{count} new · Latest ↓": (
        "{count} nuevos · Más recientes ↓", "{count} novas · Mais recentes ↓", "Новых: {count} · К последним ↓",
        "Nowe: {count} · Najnowsze ↓", "{count} neu · Neueste ↓", "{count} nouveaux · Derniers ↓",
        "{count} yeni · En son ↓", "새 메시지 {count}개 · 최신 ↓", "新着 {count}件 · 最新へ ↓", "{count} 条新消息 · 最新 ↓"),
    "Chat overlay settings": (
        "Ajustes de la superposición de chat", "Configurações da sobreposição do chat", "Настройки оверлея чата",
        "Ustawienia nakładki czatu", "Chat-Overlay-Einstellungen", "Paramètres de superposition du chat",
        "Sohbet katmanı ayarları", "채팅 오버레이 설정", "チャットオーバーレイ設定", "聊天悬浮窗设置"),
    "CHAT OVERLAY": (
        "SUPERPOSICIÓN DE CHAT", "SOBREPOSIÇÃO DO CHAT", "ОВЕРЛЕЙ ЧАТА", "NAKŁADKA CZATU", "CHAT-OVERLAY",
        "SUPERPOSITION DU CHAT", "SOHBET KATMANI", "채팅 오버레이", "チャットオーバーレイ", "聊天悬浮窗"),
    "Drag the header to move. Drag the top-right corner to resize.": (
        "Arrastra la cabecera para mover. Arrastra la esquina superior derecha para cambiar el tamaño.",
        "Arraste o cabeçalho para mover. Arraste o canto superior direito para redimensionar.",
        "Перетащите заголовок, чтобы переместить окно. Потяните правый верхний угол, чтобы изменить размер.",
        "Przeciągnij nagłówek, aby przesunąć. Przeciągnij prawy górny róg, aby zmienić rozmiar.",
        "Zum Verschieben die Kopfzeile ziehen. Zum Ändern der Größe die obere rechte Ecke ziehen.",
        "Faites glisser l’en-tête pour déplacer. Faites glisser le coin supérieur droit pour redimensionner.",
        "Taşımak için başlığı sürükleyin. Boyutlandırmak için sağ üst köşeyi sürükleyin.",
        "머리글을 드래그하여 이동하세요. 오른쪽 위 모서리를 드래그하여 크기를 조절하세요.",
        "ヘッダーをドラッグして移動します。右上の角をドラッグしてサイズを変更します。",
        "拖动标题栏可移动窗口。拖动右上角可调整大小。"),
    "Show overlay": (
        "Mostrar superposición", "Mostrar sobreposição", "Показывать оверлей", "Pokaż nakładkę", "Overlay anzeigen",
        "Afficher la superposition", "Katmanı göster", "오버레이 표시", "オーバーレイを表示", "显示悬浮窗"),
    "Lock overlay and pass mouse clicks through": (
        "Bloquear superposición y permitir clics a través", "Bloquear sobreposição e permitir cliques através",
        "Закрепить оверлей и пропускать щелчки мыши", "Zablokuj nakładkę i przepuszczaj kliknięcia myszy",
        "Overlay sperren und Mausklicks durchlassen", "Verrouiller la superposition et laisser passer les clics",
        "Katmanı kilitle ve fare tıklamalarını geçir", "오버레이 잠금 및 마우스 클릭 통과",
        "オーバーレイを固定し、マウスクリックを背後に通す", "锁定悬浮窗并让鼠标点击穿透"),
    "Opacity": (
        "Opacidad", "Opacidade", "Непрозрачность", "Nieprzezroczystość", "Deckkraft", "Opacité", "Opaklık", "불투명도", "不透明度", "不透明度"),
    "Text size": (
        "Tamaño del texto", "Tamanho do texto", "Размер текста", "Rozmiar tekstu", "Textgröße", "Taille du texte",
        "Metin boyutu", "글자 크기", "文字サイズ", "字号"),
    "Show original text below translations": (
        "Mostrar el texto original debajo de las traducciones", "Mostrar o texto original abaixo das traduções",
        "Показывать исходный текст под переводом", "Pokaż tekst oryginalny pod tłumaczeniami",
        "Originaltext unter den Übersetzungen anzeigen", "Afficher le texte original sous les traductions",
        "Çevirilerin altında özgün metni göster", "번역 아래에 원문 표시", "翻訳の下に原文を表示", "在译文下方显示原文"),
    "Channels shown in overlay": (
        "Canales mostrados en la superposición", "Canais exibidos na sobreposição", "Каналы в оверлее", "Kanały widoczne w nakładce",
        "Im Overlay angezeigte Kanäle", "Canaux affichés en superposition", "Katmanda gösterilen kanallar",
        "오버레이에 표시할 채널", "オーバーレイに表示するチャネル", "悬浮窗显示的频道"),
    "None": (
        "Ninguno", "Nenhum", "Ни одного", "Żadne", "Keine", "Aucun", "Hiçbiri", "없음", "なし", "全不选"),
    "All": (
        "Todos", "Todos", "Все", "Wszystkie", "Alle", "Tous", "Tümü", "모두", "すべて", "全选"),
    "Say": (
        "Normal", "Normal", "Общий", "Ogólny", "Sagen", "Local", "Normal", "일반", "一般", "普通"),
    "Party": (
        "Grupo", "Grupo", "Группа", "Drużyna", "Gruppe", "Groupe", "Grup", "파티", "パーティー", "队伍"),
    "Clan": (
        "Clan", "Clã", "Клан", "Klan", "Clan", "Clan", "Klan", "혈맹", "血盟", "血盟"),
    "Alliance": (
        "Alianza", "Aliança", "Альянс", "Sojusz", "Allianz", "Alliance", "İttifak", "동맹", "同盟", "同盟"),
    "Whisper": (
        "Susurro", "Sussurro", "Личные", "Szept", "Flüstern", "Privé", "Özel mesaj", "귓속말", "ささやき", "密语"),
    "Shout": (
        "Grito", "Grito", "Крик", "Krzyk", "Rufen", "Cri", "Bağırma", "외치기", "シャウト", "喊话"),
    "Trade": (
        "Comercio", "Comércio", "Торговля", "Handel", "Handel", "Commerce", "Ticaret", "거래", "トレード", "交易"),
    "World / global": (
        "Mundo / global", "Mundo / global", "Мировой / глобальный", "Światowy / globalny", "Welt / Global",
        "Monde / global", "Dünya / genel", "월드 / 전체", "ワールド / グローバル", "世界 / 全服"),
    "Hero": (
        "Héroe", "Herói", "Герои", "Bohaterowie", "Helden", "Héros", "Kahraman", "영웅", "英雄", "英雄"),
    "Command channel": (
        "Canal de mando", "Canal de comando", "Командный канал", "Kanał dowodzenia", "Kommandokanal", "Canal de commandement",
        "Komuta kanalı", "연합 채널", "指揮チャネル", "指挥频道"),
    "Other / notices": (
        "Otros / avisos", "Outros / avisos", "Прочее / уведомления", "Inne / powiadomienia", "Sonstige / Hinweise",
        "Autres / avis", "Diğer / bildirimler", "기타 / 알림", "その他 / 通知", "其他 / 通知"),
    "Changes apply immediately and are saved.\nSystem notices also need Include notices in launcher Settings.": (
        "Los cambios se aplican al instante y se guardan.\nPara los avisos del sistema, activa también Incluir avisos en los ajustes del cliente.",
        "As alterações são aplicadas e salvas imediatamente.\nPara avisos do sistema, ative também Incluir avisos nas configurações do cliente.",
        "Изменения применяются и сохраняются сразу.\nДля системных уведомлений также включите уведомления в настройках клиента.",
        "Zmiany są stosowane i zapisywane natychmiast.\nPowiadomienia systemowe wymagają też włączenia opcji uwzględniania powiadomień w ustawieniach klienta.",
        "Änderungen werden sofort angewendet und gespeichert.\nFür Systemhinweise muss auch die Option für Hinweise in den Launcher-Einstellungen aktiviert sein.",
        "Les modifications sont appliquées et enregistrées immédiatement.\nLes avis système nécessitent aussi l’option Inclure les avis dans les paramètres du client.",
        "Değişiklikler hemen uygulanır ve kaydedilir.\nSistem bildirimleri için başlatıcı ayarlarında bildirimleri dahil etme seçeneğini de açın.",
        "변경 사항은 즉시 적용되고 저장됩니다.\n시스템 알림을 보려면 런처 설정에서 알림 포함도 켜야 합니다.",
        "変更はすぐに適用され、保存されます。\nシステム通知には、ランチャー設定で通知を含める設定も必要です。",
        "更改会立即生效并保存。\n要显示系统通知，还需在启动器设置中启用包含通知。"),
    "Reset placement & unlock": (
        "Restablecer posición y desbloquear", "Redefinir posição e desbloquear", "Сбросить положение и снять закрепление",
        "Zresetuj położenie i odblokuj", "Position zurücksetzen und entsperren", "Réinitialiser la position et déverrouiller",
        "Konumu sıfırla ve kilidi aç", "위치 초기화 및 잠금 해제", "位置をリセットして固定を解除", "重置位置并解锁"),
    "Done": (
        "Listo", "Concluído", "Готово", "Gotowe", "Fertig", "Terminé", "Bitti", "완료", "完了", "完成"),
    "Overlay channels": (
        "Canales de la superposición", "Canais da sobreposição", "Каналы оверлея", "Kanały nakładki", "Overlay-Kanäle",
        "Canaux de superposition", "Katman kanalları", "오버레이 채널", "オーバーレイのチャネル", "悬浮窗频道"),
}

CATALOG = {source: dict(zip(LANGUAGES, translations)) for source, translations in _ROWS.items()}
